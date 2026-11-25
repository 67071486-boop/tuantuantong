import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { REPLICA_BY_KEY, REPLICA_ROUTES } from '../replicaRoutes'
// ⚠️ **弹层页**（`layer.open({type:2, content:'…?page=<key>'})` 打开的 iframe 页）**没有对标菜单
// 路由** ⇒ 不在 `REPLICA_ROUTES` 里（那 89 条来自对标**渲染菜单**，见 `replicaRoutes.ts` 文件头），
// 它们只靠**文件名**被 `layui-island/page.ts` 的 `import.meta.glob('./pages/*.ts')` 注册。
// 例外集从**声明这几个 key 的页面模块**取（`AR_EDIT_PAGE_KEY` / `AR_VIEW_PAGE_KEY` /
// 「设置游戏权限」页的 `SET_GAMES_PAGE_KEY`），不在这里手抄一份会漂移的清单。
import {
  AR_EDIT_PAGE_KEY,
  AR_VIEW_PAGE_KEY,
} from '../../layui-island/pages/system-roleauthority'
// 「设置游戏权限」也是**弹层页**：对标 `/User/SetGames?id=<uid>`，由人员管理页
// `layer.open({type:2})` 打开，同样没有对标菜单路由。
import { SET_GAMES_PAGE_KEY } from '../../layui-island/pages/system-setgames'
// `taker-deposit-quota` 是**自有弹层子页**
// （对标物里没有「按人 × 按游戏配押金额度」这一页）⇒ 没有对标菜单路由，与 `system-setgames`
// 同一处境：由人员管理页的 `layer.open({type:2})` 打开（`DEPOSIT_QUOTA_DIALOG_BASE`）。
// key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as TAKER_DEPOSIT_QUOTA_PAGE_KEY } from '../../layui-island/pages/taker-deposit-quota'
// ⚠️ **自有功能页**（对标物里没有这一页的）也没有对标路由 ⇒ 同样不该在 `REPLICA_ROUTES` 里。
// 与 `LAYER_ONLY_PAGE_KEYS` 同一个处理手法：key **从声明它的页面模块取**，不在这里手抄一份会漂移的清单。
// 首个成员：【第三方平台 → 闲管家店铺对接】（见
// `docs/FRONTEND-LAYOUT.md` §4 的【第三方平台】节与 `docs/DECISIONS.md` 的 `D-0073`）。
import { PAGE_KEY as XGJ_SHOP_PAGE_KEY } from '../../layui-island/pages/thirdparty-xgjshop'
// ⚠️ 同一条豁免的第三个成员：**对标有这一页、但它不在对标的 89 条渲染菜单里**
// （`/Order/OrderView` 是从订单列表 / 绩效页点开的**弹层页**）⇒ 同样不该进 `REPLICA_ROUTES`
// （那张表只收对标**渲染菜单**的 89 条）。key 同样从声明处取。
import { PAGE_KEY as ORDER_ORDERVIEW_PAGE_KEY } from '../../layui-island/pages/order-orderview'
// 鸡蛋铺系统同步那一页已删，不在本组里。
// 订单截图的两个弹层页同属这一豁免。
// 对标那边它们由父页（订单管理页 / 我的订单页）的 `layer.open({type:2})` 打开，**不在对标的
// 89 条渲染菜单里**（`OrderList20191212.js:4044-4069`、`myorderlist.js:1432-1460`）。
// 与 `system-setgames` 同一处境 ⇒ 同一豁免；key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as ORDER_SHOTS_UPLOAD_PAGE_KEY } from '../../layui-island/pages/order-screenshots-upload'
import { PAGE_KEY as ORDER_SHOTS_MANAGE_PAGE_KEY } from '../../layui-island/pages/order-screenshots-manage'
// `emp-settlementemp` 同属这条豁免：
// 对标 `/Emp/SettlementEmp?ids=<逗号串>` 是**服务端渲染的子页**，由「打手与客服报酬」页
// （`pages/emp-emplist.ts`）的 `settBtn` / `.setSett` 用 `layer.open({type:2})` 打开
// （对标触发点 `emplist.js` 的 `settSta()`）⇒ 同样**不在对标的 89 条渲染菜单里**。
// key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as EMP_SETTLEMENTEMP_PAGE_KEY } from '../../layui-island/pages/emp-settlementemp'
// `system-perfanalysis` 同属这条豁免：
// 【开放平台】是对标物里没有的一组，里面那页「性能分析」
// 自然也没有对标条目；它**有**路由（`router/index.ts` 的显式条目 `/system/performance`），
// 但不在对标的 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as SYSTEM_PERFANALYSIS_PAGE_KEY } from '../../layui-island/pages/system-perfanalysis'
// `open-apps` 同属这条豁免：
// 【开放平台】是对标物里没有的一组，「应用管理」这页**有**路由
// （`router/index.ts` 的显式条目 `/open-platform/apps`），但同样不在对标的
// 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as OPEN_APPS_PAGE_KEY } from '../../layui-island/pages/open-apps'
// `app-mp-articles`：【APP与小程序】是对标物里没有的一组（移动端频道只读、编辑 / 发布
// 放网页端；计划 `docs/plans/2026-10-23-channel-articles.md` §1）。它**有**路由
// （`router/index.ts` 的显式条目 `/app-mp/articles`），但不在对标的 89 条渲染菜单里。
// key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as APP_MP_ARTICLES_PAGE_KEY } from '../../layui-island/pages/app-mp-articles'
// `feedback`：侧栏**最底部**那一条（`docs/specs/2026-10-08-feedback-design.md` §6.1）；
// 它**有**路由（`router/index.ts` 的显式条目 `/feedback`），但同样不在对标的 89 条渲染菜单里。
// key 同样从声明处取，不在这里手抄。见 `docs/FRONTEND-LAYOUT.md` §4 的【侧栏最底部】一节。
import { PAGE_KEY as FEEDBACK_PAGE_KEY } from '../../layui-island/pages/feedback'
// `wecom-group-qr`：【第三方平台】组的「自动上传企微群码到订单截图」
// （`docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §3-A5 / §8.1）；
// 它**有**路由（`router/index.ts` 的显式条目 `/thirdparty/wecom-group-qr`），但同样不在对标的
// 89 条渲染菜单里——【第三方平台】整组都是对标物里**没有**的。key 同样从声明处取，不在这里手抄。
// 见 `docs/FRONTEND-LAYOUT.md` §4 的【第三方平台】一节。
import { PAGE_KEY as WECOM_GROUP_QR_PAGE_KEY } from '../../layui-island/pages/wecom-group-qr'
// `taker-apps`：【开放平台】组的「打手对接」；
// 它**有**路由（`router/index.ts` 的显式条目 `/open-platform/taker-apps`），但同样不在对标的
// 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。见 `docs/FRONTEND-LAYOUT.md` §4 的
// 【开放平台】一节——下面那条「每个 key 都出现在 §4 里」的判据就是它的落点。
import { PAGE_KEY as TAKER_APPS_PAGE_KEY } from '../../layui-island/pages/taker-apps'
// `self-registration`：【系统人员】组的「自助注册」（一次性注册链接的配置 / 生成 /
// 记录页，`docs/specs/2026-10-16-self-registration-links-design.md` §4 / §6）；
// 它**有**路由（`router/index.ts` 的显式条目 `/self-registration`），但同样不在对标的
// 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。见 `docs/FRONTEND-LAYOUT.md` §4 的
// 【系统人员 · 自有页】一节——下面那条「每个 key 都出现在 §4 里」的判据就是它的落点。
import { PAGE_KEY as SELF_REGISTRATION_PAGE_KEY } from '../../layui-island/pages/self-registration'
// `system-wechatmp-settings`：【第三方平台】组的「微信公众号设置」（自开发：设置页 / 回调 /
// 预设菜单 / OAuth 绑定 / 模板消息推送，计划书 `docs/plans/2026-10-19-wechatmp-official-account.md`）；
// 它**有**路由（`router/index.ts` 的显式条目 `/thirdparty/wechatmp-settings`），但同样不在对标的
// 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。见 `docs/FRONTEND-LAYOUT.md` §4 的
// 【第三方平台】一节——「每个 key 都出现在 §4 里」那条判据的落点。
import { PAGE_KEY as WECHATMP_SETTINGS_PAGE_KEY } from '../../layui-island/pages/system-wechatmp-settings'
// `qqbot-settings`：【第三方平台】组的「QQ机器人设置」（后端端点在 `api/qqbot/` 前缀）；
// 它**有**路由（`router/index.ts` 的显式条目 `/thirdparty/qqbot-settings`），但同样不在对标的
// 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。见 `docs/FRONTEND-LAYOUT.md` §4 的
// 【第三方平台】一节——下面那条「每个 key 都出现在 §4 里」的判据就是它的落点。
import { PAGE_KEY as QQBOT_SETTINGS_PAGE_KEY } from '../../layui-island/pages/qqbot-settings'
// `system-order-qr-fetch`：【第三方平台】组的「马上来查码对接」（对接「马上来 self-app」查群二维码接口）；
// 它**有**路由（`router/index.ts` 的显式条目 `/thirdparty/order-qr-fetch`），但同样不在对标的
// 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。见 `docs/FRONTEND-LAYOUT.md` §4 的
// 【第三方平台】一节。
import { PAGE_KEY as ORDER_QR_FETCH_PAGE_KEY } from '../../layui-island/pages/system-order-qr-fetch'
// `agents-withdraw-review`：打手钱包 / 提现审核是**对标物没有**的功能（对标只有「鸡蛋铺官方账号资金明细与提现」，
// 那是平台方自己的钱，没有打手钱包 / 提现审核）⇒ 本页有路由
// （`router/index.ts` 的手写条目 `/agents/withdraw-review`），但**不该**在 `REPLICA_ROUTES` 里
// （那张表只收对标 89 条渲染菜单）。key 同样从声明处取，不在这里手抄。
// 计划：`docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md` §5。
import { PAGE_KEY as AGENTS_WITHDRAW_REVIEW_PAGE_KEY } from '../../layui-island/pages/agents-withdraw-review'
// `agents-wallet-adjust`（【财务相关 → 打手钱包管理】）：自有功能页。
import { PAGE_KEY as AGENTS_WALLET_ADJUST_PAGE_KEY } from '../../layui-island/pages/agents-wallet-adjust'
// 合并页 `agents-taker-wallet`（两页签按权限渲染，两份既有页模块保留作旧深链）与
// `xgj-receipts`（闲管家收款明细，自有节点 `finance.xgj_receipt_detail`）。
// spec：`docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8。
import { PAGE_KEY as AGENTS_TAKER_WALLET_PAGE_KEY } from '../../layui-island/pages/agents-taker-wallet'
import { PAGE_KEY as XGJ_RECEIPTS_PAGE_KEY } from '../../layui-island/pages/xgj-receipts'
// 成员 `app-stats`（【APP与小程序 → 数据概览】）。
// 它**有**路由 `/business/app-stats`，但同样是**自有功能**（对标物里没有这一组）⇒ 与另几个成员
// 受**同一条**判据约束：**必须在 `docs/FRONTEND-LAYOUT.md` §4 里有一段自己的说明**。
// ⚠️ 权限点**复用** `finance.support_statistics`（计划 §8 第一条：⛔ 不新增节点）。
import { PAGE_KEY as APP_STATS_PAGE_KEY } from '../../layui-island/pages/app-stats'
// `order-pushjdp`：对标 `/Order/PushJdp2026?orderId=<内部单号>` 是列表页「操作」列地球按钮
// （`.pushJdpEdit`）用 `layer.open({type:2})` 打开的**独立 iframe 页**
// （`OrderList20191212.js` L4683-4694）
// ⇒ 不在对标的 89 条渲染菜单里。key 同样从声明处取，不在这里手抄。
import { PAGE_KEY as ORDER_PUSHJDP_PAGE_KEY } from '../../layui-island/pages/order-pushjdp'
// 成员 `dailiantong-stats`（代练通按日统计）与
// `dailiantong-order-templates`（代练通订单要求模板）。两页都是**自有功能**（对标物里没有），
// 有路由（`/system/dailiantong-stats` / `/system/dailiantong-order-templates`），
// 权限点**复用** `order.list`（⛔ 不新增 RBAC 节点）⇒ 与另几个成员受**同一条**判据约束：
// **必须在 `docs/FRONTEND-LAYOUT.md` §4 里各有一段自己的说明**。
import { PAGE_KEY as DAILIANTONG_STATS_PAGE_KEY } from '../../layui-island/pages/dailiantong-stats'
import { PAGE_KEY as DAILIANTONG_ORDER_TEMPLATES_PAGE_KEY } from '../../layui-island/pages/dailiantong-order-templates'

/**
 * 不需要对标菜单路由元数据的 island 页 key。
 *
 * 共同点是「**不在对标的 89 条渲染菜单里**」：`order-orderview` 另有一条
 * **我们自建**的 `/orders/:no` 路由，`system-setgames` / `taker-deposit-quota` 由人员管理页的
 * `layer.open({type:2})` 打开，两个角色权限页则完全没有路由，
 * `order-screenshots-upload` / `order-screenshots-manage` 由订单管理页 / 我的订单页的
 * `.upImg` / `.mangeImg` 打开，`emp-settlementemp` 由绩效页的两颗结算入口打开，
 * `order-pushjdp` 由订单列表「操作」列的地球按钮打开。
 */
const LAYER_ONLY_PAGE_KEYS = new Set([
  AR_EDIT_PAGE_KEY,
  AR_VIEW_PAGE_KEY,
  SET_GAMES_PAGE_KEY,
  TAKER_DEPOSIT_QUOTA_PAGE_KEY,
  ORDER_ORDERVIEW_PAGE_KEY,
  ORDER_SHOTS_UPLOAD_PAGE_KEY,
  ORDER_SHOTS_MANAGE_PAGE_KEY,
  EMP_SETTLEMENTEMP_PAGE_KEY,
  ORDER_PUSHJDP_PAGE_KEY,
])

/**
 * **自有功能** island 页（有路由，但**没有对标条目**）⇒ 不该在 `REPLICA_ROUTES` 里。
 *
 * ⚠️ 与 `LAYER_ONLY_PAGE_KEYS` 的区别是**为什么**没有表项：那一组的判据是
 * 「**不在对标的 89 条渲染菜单里**」（两个角色权限页没有路由，`order-orderview` 有我们自建的路由），
 * 这一组是「**对标物里根本没有这一页**」。两组都**不能**当成"想加就加"的豁免：
 * 加进来的每一项都必须在 `docs/FRONTEND-LAYOUT.md` §4 里有一段自己的说明（弹层类页面登记在
 * §4 的【弹层子页】专节，不塞进 IA 骨架——§4 原本是信息架构骨架，不是页面索引）。
 * 下面那条用例按**两个豁免集合的并集**逐 key 钉住 §4 段落（`readFileSync` 读 §4）
 * ⇒ 新增成员漏写一段**会红**。
 * ⚠️ `order-orderview` 的**差异**登记处是 `docs/FRONTEND-REPLICA-STANDARD.md` §7，不重复同一事实。
 *
 * 成员以 `OWN_FEATURE_PAGE_KEYS` 那个字面集合为准。每个 key 仍要在版面文档 §4 有一段自己的说明。
 */
const OWN_FEATURE_PAGE_KEYS = new Set([
  XGJ_SHOP_PAGE_KEY,
  SYSTEM_PERFANALYSIS_PAGE_KEY,
  OPEN_APPS_PAGE_KEY,
  // 成员 `feedback`（【侧栏最底部 → 问题反馈】）：**有**路由 `/feedback`，但对标物侧栏最底部
  // 没有这一条 ⇒ 同受「必须在 `docs/FRONTEND-LAYOUT.md` §4 里有一段自己的说明」约束
  // （见【侧栏最底部】一节）。⚠️ 权限点是**精确命中**的 `home.feedback`
  // （对标 112 号点「问题反馈及建议」），⛔ 不是复用别的点（见 `config/routePermissions.ts`）。
  FEEDBACK_PAGE_KEY,
  // 成员 `wecom-group-qr`（【第三方平台 → 自动上传企微群码到订单截图】）：**有**路由
  // `/thirdparty/wecom-group-qr`，对标物里没有【第三方平台】这一组 ⇒ 必须在 §4【第三方平台】
  // 一节有一段说明。⚠️ 权限点**复用** `system.pinduoduo_settings`（⛔ 不新增 RBAC 节点），
  // 「谁能看到这一页」由**第二道门**收窄（路由级 `requiresAccountAdmin` + 页内自检）
  // ——逐条理由与代价见 `config/routePermissions.ts` 里那条登记。
  WECOM_GROUP_QR_PAGE_KEY,
  // 成员 `taker-apps`（【开放平台 → 打手对接】）：**有**路由 `/open-platform/taker-apps`，
  // 对标物里没有打手侧开放接口 ⇒ 必须在 §4【开放平台】一节有一段说明。
  // ⚠️ 权限点是**自有节点** `system.allow_taker_open_api`（`benchmark_id = None`，⛔ 不是复用别人的点）。
  TAKER_APPS_PAGE_KEY,
  // 成员 `self-registration`（【系统人员 → 自助注册】）：**有**路由 `/self-registration`，
  // 对标物的人员管理组没有这一页 ⇒ 必须在 §4【系统人员 · 自有页】一节有一段说明。
  // ⚠️ 权限点**复用** `system_user.add_edit`（与「新开系统账号 / 编辑系统账号」同一个点，
  // ⛔ 不新增 RBAC 节点）；逐条理由与代价见 `config/routePermissions.ts` 里那条登记。
  SELF_REGISTRATION_PAGE_KEY,
  // 成员 `system-wechatmp-settings`（【第三方平台 → 微信公众号设置】）：**有**路由
  // `/thirdparty/wechatmp-settings` ⇒ 必须在 §4【第三方平台】一节有一段说明。
  // ⚠️ 权限点**复用** `system.pinduoduo_settings`（⛔ 不新增 RBAC 节点）；
  // 「谁能看到这一页」由**第二道门**收窄（路由级 `requiresAccountAdmin` + 页内自检）。
  WECHATMP_SETTINGS_PAGE_KEY,
  // 成员 `qqbot-settings`（【第三方平台 → QQ机器人设置】）：**有**路由 `/thirdparty/qqbot-settings`
  // ⇒ 必须在 §4【第三方平台】一节有一段说明。⚠️ 权限点是**自有节点** `system.qqbot_settings`
  // （能单独授权）；「谁能看到这一页」另由**第二道门**收窄（路由级 `requiresAccountAdmin` + 页内自检）。
  QQBOT_SETTINGS_PAGE_KEY,
  // 成员 `agents-withdraw-review`（【财务相关 → 打手提现审核】）：**有**路由
  // `/agents/withdraw-review`，对标物里没有打手钱包 / 提现审核 ⇒ 必须在 §4 里有一段自己的说明。
  // ⚠️ 权限点是**自有节点** `finance.taker_withdraw_review`（`benchmark_id = None`，能单独授权）。
  AGENTS_WITHDRAW_REVIEW_PAGE_KEY,
  // 成员 `agents-wallet-adjust`（【财务相关 → 打手钱包管理】）：**有**路由 `/agents/wallet-adjust`、
  // 权限点是自有节点 `finance.taker_wallet_adjust`。⚠️ 菜单条目已并入下面的合并页，
  // 本 key 的路由保留作旧深链。
  AGENTS_WALLET_ADJUST_PAGE_KEY,
  // 成员 `agents-taker-wallet`（【财务相关 → 打手钱包】
  // 合并页：提现审核 / 钱包管理两个页签，**按权限渲染**——任一节点即见菜单（some 语义），
  // 只有一个节点的人只见对应页签）。两份既有页模块（withdraw-review / wallet-adjust）
  // **文件保留**、旧路由照旧活着作旧深链；它的两个权限点即上面那两条自有节点。
  AGENTS_TAKER_WALLET_PAGE_KEY,
  // 成员 `xgj-receipts`（【财务相关 → 闲管家收款明细】，
  // 闲管家「交易成功」推送的记账与按店铺统计页）。权限点是**自有节点**
  // `finance.xgj_receipt_detail`（`benchmark_id = None`，能单独授权）。
  // spec：`docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §6/§8.1。
  XGJ_RECEIPTS_PAGE_KEY,
  // 成员 `app-mp-articles`
  // （【APP与小程序 → 发布文章作品】）。自有功能页，路由 `/app-mp/articles`；
  // 权限点是自有节点 `system.article_manage`。它同样必须在 `docs/FRONTEND-LAYOUT.md` §4
  // 有一段自己的说明（见【APP与小程序】一节）。
  APP_MP_ARTICLES_PAGE_KEY,
  // 成员 `app-stats`
  // （【APP与小程序 → 数据概览】）。自有功能页，路由 `/business/app-stats`。
  APP_STATS_PAGE_KEY,
  // 成员 `dailiantong-stats`
  // （【发平台设置 → 代练通按日统计】）与 `dailiantong-order-templates`
  // （【发平台设置 → 代练通订单要求模板】）。两页都是自有功能页
  // （对标物里没有），权限点**复用** `order.list`。
  DAILIANTONG_STATS_PAGE_KEY,
  DAILIANTONG_ORDER_TEMPLATES_PAGE_KEY,
  // 成员 `system-order-qr-fetch`
  // （【第三方平台 → 马上来查码对接】）。自有功能页，路由 `/thirdparty/order-qr-fetch`；
  // 权限点**复用** `system.pinduoduo_settings`（微信公众号设置先例；第二道门
  // `requiresAccountAdmin` + 页内自检）。§4 说明段见【第三方平台】一节。
  ORDER_QR_FETCH_PAGE_KEY,
])

/** 对标路由 ↔ 我们的 island 页 key/路由的映射表。
 *
 *  ⚠️ **本测试不得读 `.tools/`**（那目录不进 git，CI/别人的克隆里没有）。生成器已废止
 *  （`docs/DECISIONS.md` 的 D-0062 方案 A）：它读离线勘察产物（`routes.json` / `routes_probe_*.json` /
 *  `manifest.json` / `home.html` / `i18n_zh.json`）把 89 条逐条固化进 `replicaRoutes.ts` 的那套
 *  **生成期对拍不再存在**。本文件只断言**固化后的不变量**：数量、唯一性、与真路由表不冲突、
 *  key 命名规则、已存在的 island 页文件都有表项（弹层页见 `LAYER_ONLY_PAGE_KEYS`，它们没有路由）。
 *  `replicaRoutes.ts` 文件头注释里写清了每个字段的取值来源与特例。
 */
describe('对标路由映射表（replicaRoutes）', () => {
  it('恰好 89 条（84 可达 + 5 被拒）', () => {
    // 出处：.tools/recon/routes.json = 89；.tools/recon/now/routes_probe_*.json
    // 实测 84 条 200 + 5 条 302 → /ErrorPage/nopermission。数量早已在原始勘察中确定
    //（`.tools/recon/routes.json` = 89，该目录不进 git），这里防手改。
    expect(REPLICA_ROUTES.length).toBe(89)
    expect(REPLICA_ROUTES.filter((r) => r.accessDenied).length).toBe(5)
    expect(REPLICA_ROUTES.filter((r) => !r.accessDenied).length).toBe(84)
  })

  it('benchmarkHref / key / routeName / path 各自唯一', () => {
    for (const field of ['benchmarkHref', 'key', 'routeName', 'path'] as const) {
      const values = REPLICA_ROUTES.map((r) => r[field])
      expect(new Set(values).size, `${field} 有重复`).toBe(values.length)
    }
  })

  it('accessDenied 的恰好是实测被拒的那 5 条（集合断言，不是数量断言）', () => {
    // 集合断言：数量对但换了条目也要红。实测出处：.tools/recon/now/routes_probe_*.json
    //（code=302、redirect=/ErrorPage/nopermission）。
    const denied = REPLICA_ROUTES.filter((r) => r.accessDenied).map((r) => r.benchmarkHref)
    expect(new Set(denied)).toEqual(
      new Set([
        '/Order/JdpShopOrderList',
        '/jdpshop/shopindex',
        '/jdpshop/shoptype',
        '/jdpshop/addshopitem',
        '/jdpshop/shopmange',
      ]),
    )
  })

  it('key 命名规则：除 system-user-list 外全部是 kebab-case 的多段 key', () => {
    // key = 小写 <controller>-<page>；`/User/userlist` → `system-user-list` 是唯一例外
    //（该页已上线，key 不能改——`src/layui-island/page.ts` 的注册表按文件名取 key）。
    // 写法说明：`vitest/no-conditional-expect` 禁止在 if 里 expect，所以用 filter 把
    // 「例外」与「其余」先分成两张列表，再各自断言。
    const special = REPLICA_ROUTES.filter((r) => r.key === 'system-user-list')
    expect(special.map((r) => r.benchmarkHref)).toEqual(['/User/userlist'])
    const regular = REPLICA_ROUTES.filter((r) => r.key !== 'system-user-list')
    expect(regular.length).toBe(REPLICA_ROUTES.length - 1)
    for (const r of regular) {
      expect(r.key, `${r.benchmarkHref} 的 key 不合规则`).toMatch(/^[a-z0-9]+(-[a-z0-9]+)+$/)
    }
  })

  it('key 与 benchmarkHref 同源：controller-page 小写化，唯一例外见上', () => {
    // 这条让「key 照抄对标拼写」可被机械校验：改 href 忘了改 key（或反之）会红。
    const mismatched = REPLICA_ROUTES.filter((r) => r.key !== 'system-user-list').filter((r) => {
      const [controller, page] = r.benchmarkHref.replace(/^\//, '').split('/')
      return r.key !== `${controller}-${page}`.toLowerCase()
    })
    expect(mismatched.map((r) => r.benchmarkHref)).toEqual([])
  })

  it('/User/userlist 必须指向既有路由 /system-users（不新建重复路由）', () => {
    const r = REPLICA_BY_KEY['system-user-list']
    expect(r, 'REPLICA_BY_KEY 里没有 system-user-list').toBeDefined()
    expect(r!.benchmarkHref).toBe('/User/userlist')
    expect(r!.path).toBe('/system-users')
    expect(r!.routeName).toBe('system-users')
    expect(r!.hasBackend).toBe(true)
  })

  it('path 不与 router/index.ts 的既有字面路径冲突（唯一例外 /system-users）', () => {
    // ⚠️ 下面这份清单是**手抄**的——`router/index.ts` 读不到结构化数据，所以只能抄。
    // 抄的是 `createRouter({ … routes: [ … ] })` 里**显式条目**的 `path:` 字面量
    // （映射表生成的那批不在内：它们来自本测试自己的断言面）。它过期时靠本测试红来发现。
    // ⚠️ **不要抄行号**：本文件上一版抄过，一次注释改动就整体漂移（抄的 `'/'` 早已不在原行）。
    // 定位一律用构造名（`routes` 数组里的 `path: '…'`），行号会漂移、构造不会。
    const ROUTER_INDEX_PATHS: string[] = [
      '/',
      '/orders',
      '/orders/new',
      '/orders/:order_no(\\d{20})',
      '/game-config/games',
      // 分类页与项目页合并 ⇒ `game-config-categories` 路由已删除（不在本清单）。
      '/game-config/projects',
      // 自建页 `/agents/withdraw-review`
      // （不在映射表里，抄进来是为了「表项不许撞上显式路径」这条判据不因它变瞎）。
      '/agents/withdraw-review',
      '/login',
      '/system-users', // ← 唯一允许复用：/User/userlist 的已上线页
      '/system-users/new',
      '/system-users/:id/edit',
      '/system-users/login-restrict',
      '/system-users/blacklist',
      '/system-users/real-name',
      '/system-users/contract',
      '/:pathMatch(.*)*',
    ]
    const existing = new Set(ROUTER_INDEX_PATHS)
    // 复用既有路径的只允许是 /system-users（= /User/userlist 的已上线页）。
    const reused = REPLICA_ROUTES.filter((r) => existing.has(r.path))
    expect(reused.map((r) => r.path)).toEqual(['/system-users'])
    expect(reused.map((r) => r.routeName)).toEqual(['system-users'])
    // 其余任何一条撞上既有字面路径都要红。
    const conflicts = REPLICA_ROUTES.filter((r) => r.path !== '/system-users' && existing.has(r.path))
    expect(conflicts.map((r) => r.path)).toEqual([])
  })

  it('routeName 也不与既有 name 撞车（唯一例外 system-users）', () => {
    // 与上一条同理，手抄自 `src/frontend/src/router/index.ts` 里 `routes` 数组每条路由的
    // `name:` 字面量——同样**不写行号**（行号会漂移，构造名不会）：
    // `home` / `orders` / `order-new` / `order-detail` / `game-config-games` /
    // `game-config-projects` / `login` / `system-users` /
    // `system-user-new` / `system-user-edit` / `system-login-restrict` /
    // `system-blacklist` / `system-real-name` / `system-contract` / `not-found`。
    // `system-onboarding` / `system-checkin-log` / `system-checkin-static` 三条显式路由已随业务裁剪
    // 删除（D-0036），所以不在本清单。
    const EXISTING_ROUTE_NAMES = new Set([
      'home',
      'orders',
      'order-new',
      'order-detail',
      'game-config-games',
      // `game-config-categories` 随分类 / 项目合并删除，不再占一个 name。
      'game-config-projects',
      // 自建页（不在映射表）的 name：`agents-withdraw-review`、`agents-wallet-adjust`、
      // `agents-taker-wallet`（合并页）与 `xgj-receipts`（收款明细）。
      'agents-withdraw-review',
      'agents-wallet-adjust',
      'agents-taker-wallet',
      'xgj-receipts',
      // `app-stats` 自建页的 name。
      'app-stats',
      'login',
      'system-users',
      'system-user-new',
      'system-user-edit',
      'system-login-restrict',
      'system-blacklist',
      'system-real-name',
      'system-contract',
      'not-found',
    ])
    // 与既有 name 撞车的必须恰好是 system-users，且它必须就是 /system-users 那条。
    const reusedNames = REPLICA_ROUTES.filter((r) => EXISTING_ROUTE_NAMES.has(r.routeName))
    expect(reusedNames.map((r) => `${r.routeName} → ${r.path}`)).toEqual(['system-users → /system-users'])
  })

  it('hasBackend：恰好是下面十六条（2026-10-28 起加入 D-0319 的结算记录按天聚合页）', () => {
    // 「已登记翻真」的判据见文件头注释——⚠️ 它**不是**「后端已接的页面集合」：
    // `order-insiderecorderlist`（`@/api/hall`）与 `system-sourelist`（`@/api/orderSources`）
    // 都已有真接口却仍是 `false`（各自线的既有裁决，见 docs/plans/ 那两条线的计划）。
    // 本表各条的现状与出处：
    // `/User/myinfo` 自挂载 + 接 `/api/auth/profile` / `/api/auth/password`
    // （见 `docs/specs/2026-09-24-my-profile-design.md` §6）、
    // `/Order/orderrecycle` 自建岛页 + 接回收站列表 / 还原两个端点
    // （见 `docs/plans/2026-09-24-order-delete-and-filters.md`）、
    // `/Order/TradingOrderActionLogList` 自挂载 + 接 `GET /api/audit/order-action-logs/`（见
    // `docs/specs/2026-10-04-trade-action-log-design.md` §5——⛔ 不能继续用 `artifactPage`：
    // 它的捕获阶段兜底会把本页工具栏那颗 `lay-event="refresh"` 按钮吞掉并弹「尚未开放」）、
    // `/User/userlist`（接 `/api/accounts/`）、`/System/pddshipsetting`（自挂载 + 真数据，
    // 见 `docs/plans/2026-09-21-shop-binding-platform-order-read.md`）、
    // `/System/homesystemsetting`（自挂载 + 接 `/api/business/peer-bindings/` 的三个端点，
    // 见 `docs/specs/2026-09-25-upstream-downstream-order-sync-design.md` §11）、
    // `/User/RnameCertLogAndSetting`（自挂载 + 接 `GET /api/accounts/realname-logs/`、
    // `GET`/`PATCH /api/accounts/realname-setting/` 与 `GET /api/accounts/realname-quota/`，
    // 见 `docs/specs/2026-10-10-realname-verification-design.md` §10.1——同样**不能**继续用
    // `artifactPage`：本页工具栏三颗按钮都是 `lay-event`）、
    // `/Order/SoureOrderChangePriceLog`（自挂载 + 接 `GET /api/business/upstream-price-change-logs/`，
    // 见 `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §D4——同样**不能**
    // 继续用 `artifactPage`：本页要真取数、真筛选、真开详情层，而它的契约是「表格恒空、不发请求」）、
    // `/System/OrderPushGroupMsgSet` 与 `/System/systemnotice`（都改成自挂载 + 真端点）、
    // `/System/PlatformAutoReply`（自挂载 + 接 `/api/business/platform-auto-replies/` 的四个动作
    // ——列表 / 新增 / 修改 / 删除；行内开关与批量改内容也走 PATCH。⛔ 同样**不能**继续用
    // `artifactPage`：它的契约是「表格恒空、不发写请求」，而本页要真取数、真开弹层）、
    // `/Emp/billsummary`（整页重设计为「按天聚合」**自建岛页**，对标复刻形态整体下线：
    // 主视图接 `GET /api/business/settlements/daily-summary`、明细复用 `/settlements`）。
    // ⚠️ 这是**恰好集合**断言（不是 toContain）：将来再翻真一条必须同一次补进来，不许放宽成 toContain。
    const withBackend = REPLICA_ROUTES.filter((r) => r.hasBackend).map((r) => r.benchmarkHref)
    // 集合顺序 = `REPLICA_ROUTES` 的数组顺序（对标菜单序），不是「翻真的时间序」。
    expect(withBackend).toEqual([
      '/User/myinfo',
      '/Order/CoreOrderCenter',
      '/Order/orderrecycle',
      '/Order/TradingOrderActionLogList',
      '/Order/SoureOrderChangePriceLog',
      '/User/userlist',
      '/User/RnameCertLogAndSetting',
      '/Emp/billsummary',
      '/Emp/salesmanstatistics',
      '/Emp/dayoperation',
      '/System/tripartiteplatformsetting',
      '/System/PlatformAutoReply',
      '/System/OrderPushGroupMsgSet',
      '/System/homesystemsetting',
      '/System/pddshipsetting',
      '/System/systemnotice',
    ])
  })

  it('/User/RnameCertLogAndSetting 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 本页从 `artifactPage` 改成自挂载 + 接三个端点
    // （列表 / 那四项配置的读+写 / 余量）⇒ 与上面六条同一次登记翻真。
    const r = REPLICA_BY_KEY['user-rnamecertlogandsetting']
    expect(r, 'REPLICA_BY_KEY 里没有 user-rnamecertlogandsetting').toBeDefined()
    expect(r!.benchmarkHref).toBe('/User/RnameCertLogAndSetting')
    expect(r!.path).toBe('/accounts/real-name')
    expect(r!.hasBackend).toBe(true)
  })

  it('/User/myinfo 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 上一条只说明「集合恰好是这四条」；这条钉住**是哪一行**为 true——将来集合断言被放宽成
    // toContain 时，四条各自仍然被钉住。
    const r = REPLICA_BY_KEY['user-myinfo']
    expect(r, 'REPLICA_BY_KEY 里没有 user-myinfo').toBeDefined()
    expect(r!.benchmarkHref).toBe('/User/myinfo')
    expect(r!.path).toBe('/accounts/profile')
    expect(r!.hasBackend).toBe(true)
  })

  it('/Order/orderrecycle 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 上一条只说明「集合恰好是这三条」；这条钉住**是哪一行**为 true——将来集合断言被放宽成
    // toContain 时，三条各自仍然被钉住。
    const r = REPLICA_BY_KEY['order-orderrecycle']
    expect(r, 'REPLICA_BY_KEY 里没有 order-orderrecycle').toBeDefined()
    expect(r!.benchmarkHref).toBe('/Order/orderrecycle')
    expect(r!.path).toBe('/orders/recycle')
    expect(r!.hasBackend).toBe(true)
  })

  it('/System/pddshipsetting 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 上一条只说明「集合恰好是这两条」；这条钉住**是哪一行**为 true——将来集合断言被放宽成
    // toContain 时，两条各自仍然被钉住（`system-user-list` 另见上面那条既有断言）。
    const r = REPLICA_BY_KEY['system-pddshipsetting']
    expect(r, 'REPLICA_BY_KEY 里没有 system-pddshipsetting').toBeDefined()
    expect(r!.benchmarkHref).toBe('/System/pddshipsetting')
    expect(r!.path).toBe('/system/pdd-ship')
    expect(r!.hasBackend).toBe(true)
  })

  it('/System/homesystemsetting 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 本页从 `artifactPage` 改成自挂载 + 接
    // `/api/business/peer-bindings/` 三个端点 ⇒ 与上面四条同一次登记翻真。
    const r = REPLICA_BY_KEY['system-homesystemsetting']
    expect(r, 'REPLICA_BY_KEY 里没有 system-homesystemsetting').toBeDefined()
    expect(r!.benchmarkHref).toBe('/System/homesystemsetting')
    expect(r!.path).toBe('/system/source-system')
    expect(r!.hasBackend).toBe(true)
  })

  it('/Order/TradingOrderActionLogList 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 本页从 `artifactPage` 改成自挂载 + 接
    // 单端点 `GET /api/audit/order-action-logs/`（两张表共用同一次响应）⇒ 与上面五条同一次登记翻真。
    const r = REPLICA_BY_KEY['order-tradingorderactionloglist']
    expect(r, 'REPLICA_BY_KEY 里没有 order-tradingorderactionloglist').toBeDefined()
    expect(r!.benchmarkHref).toBe('/Order/TradingOrderActionLogList')
    expect(r!.path).toBe('/orders/platform-action-log')
    expect(r!.hasBackend).toBe(true)
  })

  it('/Order/SoureOrderChangePriceLog 翻真钉点（只增钉点，不改上面那条集合断言）', () => {
    // 本页从 `artifactPage` 静态页改成自挂载 +
    // 接单端点 `GET /api/business/upstream-price-change-logs/`（真取数 / 真筛选 / 真开详情层）
    // ⇒ 与上面七条同一次登记翻真。
    const r = REPLICA_BY_KEY['order-soureorderchangepricelog']
    expect(r, 'REPLICA_BY_KEY 里没有 order-soureorderchangepricelog').toBeDefined()
    expect(r!.benchmarkHref).toBe('/Order/SoureOrderChangePriceLog')
    expect(r!.path).toBe('/orders/source-price-log')
    expect(r!.hasBackend).toBe(true)
  })

  it('/Emp/salesmanstatistics 翻真钉点（2026-10-17 起自有实现；只增钉点，不改上面那条集合断言）', () => {
    // 本页原先走 `artifactPage` 渲染证据件、零 API 调用；2026-10-17 改成自建岛页 +
    // 接 `GET /api/business/support-stats`（spec §4）⇒ 与上面七条同一次登记翻真。
    const r = REPLICA_BY_KEY['emp-salesmanstatistics']
    expect(r, 'REPLICA_BY_KEY 里没有 emp-salesmanstatistics').toBeDefined()
    expect(r!.benchmarkHref).toBe('/Emp/salesmanstatistics')
    expect(r!.path).toBe('/agents/salesman-stats')
    expect(r!.hasBackend).toBe(true)
    // `note` 要点名「2026-10-17 起自有实现」（计划 §6 第 3 条）。
    expect(r!.note).toContain('2026-10-17 起自有实现')
  })

  it('/Emp/billsummary 翻真钉点（2026-10-28 D-0319 按天聚合自建岛页；只增钉点，不改上面那条集合断言）', () => {
    // 本页原先是对标 `/Emp/billsummary` 的像素级复刻页（证据件 `replica/emp-billsummary.html`）；
    // 按 D-0319 整页重设计为「按天聚合」**自建岛页**（复刻形态整体下线）：
    // 主视图接 `GET /api/business/settlements/daily-summary`、明细复用 `/settlements`。
    // 路由 / 菜单 / 权限节点全部不变（`finance.settlement_records` 可见、`finance.payroll` 读闸）。
    const r = REPLICA_BY_KEY['emp-billsummary']
    expect(r, 'REPLICA_BY_KEY 里没有 emp-billsummary').toBeDefined()
    expect(r!.benchmarkHref).toBe('/Emp/billsummary')
    expect(r!.path).toBe('/agents/settlement')
    expect(r!.routeName).toBe('agents-settlement')
    expect(r!.hasBackend).toBe(true)
    expect(r!.note).toContain('D-0319')
  })

  it('/Emp/dayoperation 翻真钉点（2026-10-17 重新启用；只增钉点，不改上面那条集合断言）', () => {
    // 本页 2026-10-17 之前**没有页面**、还被 `router/index.ts` 的 `REPLICA_SKIP` 排除；
    // 本线把它摘出来（映射表自动生成 `/agents/day-report`）并新增自建岛页
    // `emp-dayoperation.ts`（spec §5 / §7）。
    const r = REPLICA_BY_KEY['emp-dayoperation']
    expect(r, 'REPLICA_BY_KEY 里没有 emp-dayoperation').toBeDefined()
    expect(r!.benchmarkHref).toBe('/Emp/dayoperation')
    expect(r!.path).toBe('/agents/day-report')
    expect(r!.hasBackend).toBe(true)
  })

  it('title / benchmarkMenuLabel 非空，shape 取值在约定集合内', () => {
    const SHAPES = new Set(['list', 'form', 'dashboard', 'tabs', 'doc', 'unknown'])
    for (const r of REPLICA_ROUTES) {
      expect(r.title.length, `${r.benchmarkHref} 的 title 为空`).toBeGreaterThan(0)
      // 89/89 由「渲染菜单 × i18n 词典」解出（策略见 replicaRoutes.ts 文件头）；
      // 将来真出现解不出的条目时，这里会红——那时按约定改成允许 null + note，别静默放宽。
      expect(typeof r.benchmarkMenuLabel, `${r.benchmarkHref} 的菜单名解不出`).toBe('string')
      expect(r.benchmarkMenuLabel!.length).toBeGreaterThan(0)
      expect(SHAPES.has(r.shape), `${r.benchmarkHref} 的 shape=${r.shape} 不在约定集合内`).toBe(true)
    }
  })

  it('第三方平台订单管理：改名后的 title / 菜单名（T7，2026-10-26；T11a 起接真）', () => {
    // 业务方要求把对标「交易平台订单处理」做回来、菜单改名「第三方平台订单管理」；
    // 已接真后端（`GET /api/business/platform-pushes/`）⇒ hasBackend 翻 true，
    // note 与实现同步。
    // ⚠️ `benchmarkTitle` 是**对标页自己的 <title>**（实测转录），不随我方菜单名改。
    const r = REPLICA_BY_KEY['order-coreordercenter']
    expect(r, 'REPLICA_BY_KEY 里没有 order-coreordercenter').toBeDefined()
    expect(r!.benchmarkTitle).toBe('交易平台订单处理')
    expect(r!.title).toBe('第三方平台订单管理')
    expect(r!.benchmarkMenuLabel).toBe('第三方平台订单管理')
    expect(r!.routeName).toBe('orders-platform-core')
    expect(r!.path).toBe('/orders/platform-core')
    expect(r!.hasBackend).toBe(true)
    expect(r!.accessDenied).toBe(false)
    expect(r!.note).toContain('2026-10-27 起接真后端')
    expect(r!.note).not.toContain('无后端')
  })

  it('团团通资金明细与提现：改名后的 title / 菜单名（2026-10-30，D-0339；只动显示层）', () => {
    // 业务方 2026-10-30 指令：显示名「鸡蛋铺资金明细与提现」改「团团通资金明细与提现」
    //（菜单文案 + 页面标题；对标 key / 路由 / 权限点 / 功能零改动）。
    // ⚠️ `benchmarkTitle` 是**对标页自己的 <title>**（实测转录），不随我方菜单名改——
    // 与上面 T7 那条同款钉法；`benchmarkMenuLabel` 是 `meta.title`（页签条）的来源 ⇒ 随我方名走。
    const r = REPLICA_BY_KEY['emp-funddetailslist']
    expect(r, 'REPLICA_BY_KEY 里没有 emp-funddetailslist').toBeDefined()
    expect(r!.benchmarkTitle).toBe('鸡蛋铺资金明细与提现')
    expect(r!.title).toBe('团团通资金明细与提现')
    expect(r!.benchmarkMenuLabel).toBe('团团通资金明细与提现')
    expect(r!.routeName).toBe('agents-fund-detail')
    expect(r!.path).toBe('/agents/fund-detail')
    expect(r!.hasBackend).toBe(false)
    expect(r!.accessDenied).toBe(false)
    expect(r!.note).toContain('业务方 2026-10-30 指令改名')
  })

  it('交易平台对接设置：T12 起接真（hasBackend 翻 true，note 写现状）', () => {
    const r = REPLICA_BY_KEY['system-tripartiteplatformsetting']
    expect(r, 'REPLICA_BY_KEY 里没有 system-tripartiteplatformsetting').toBeDefined()
    expect(r!.benchmarkHref).toBe('/System/tripartiteplatformsetting')
    expect(r!.path).toBe('/system/platform-integration')
    expect(r!.hasBackend).toBe(true)
    expect(r!.note).toContain('2026-10-27 起接真后端')
    expect(r!.note).not.toContain('无后端')
  })

  it('REPLICA_BY_KEY 与 REPLICA_ROUTES 一一对应', () => {
    // 注册表自检用（`key → ReplicaRoute`）：漏一条或多一条都会让 island 宿主拿不到元数据。
    expect(Object.keys(REPLICA_BY_KEY).length).toBe(REPLICA_ROUTES.length)
    for (const r of REPLICA_ROUTES) {
      expect(REPLICA_BY_KEY[r.key], `${r.key} 不在 REPLICA_BY_KEY 里`).toBe(r)
      expect(REPLICA_BY_KEY[r.key]!.benchmarkHref).toBe(r.benchmarkHref)
    }
  })

  it('除弹层页与自有功能页外，已存在的 island 页文件（layui-island/pages/*.ts）都有表项', () => {
    // 不要求反向（表项都有文件——大部分页面还没实现）。key 的来源是文件名，见
    // `src/layui-island/page.ts` 的 `import.meta.glob('./pages/*.ts')`（只扫一层，
    // `pages/__tests__/` 不会被扫到，所以这里也只看文件、不递归）。
    // 弹层页（`LAYER_ONLY_PAGE_KEYS`）豁免：它们没有路由，也就没有可登记的对标路由。
    // 自有功能页（`OWN_FEATURE_PAGE_KEYS`）豁免：有路由，但**对标物里没有这一页**。
    const pagesDir = fileURLToPath(new URL('../../layui-island/pages', import.meta.url))
    const pageKeys = fs
      .readdirSync(pagesDir, { withFileTypes: true })
      .filter((e) => e.isFile() && e.name.endsWith('.ts'))
      .map((e) => e.name.replace(/\.ts$/, ''))
    expect(pageKeys.length, 'layui-island/pages 下应有已实现的页面文件').toBeGreaterThan(0)
    // 聚合成一条断言：逐条 expect 会触发 `vitest/no-conditional-expect`（列表非空时才有意义）。
    const missing = pageKeys.filter(
      (key) =>
        !LAYER_ONLY_PAGE_KEYS.has(key) &&
        !OWN_FEATURE_PAGE_KEYS.has(key) &&
        REPLICA_BY_KEY[key] === undefined,
    )
    expect(missing, `已实现的 island 页在映射表里没有表项：${missing.join(', ')}`).toEqual([])
  })

  it('两个豁免集合的每个 key 都出现在 docs/FRONTEND-LAYOUT.md §4 里', () => {
    // `OWN_FEATURE_PAGE_KEYS` / `LAYER_ONLY_PAGE_KEYS` 的注释要求「加进来的每一项都必须在 §4 里
    // 有一段自己的说明」（自有功能进对应组节、弹层页进 §4 的【弹层子页】专节）。本用例把那条要求
    // 变成**能失败**的判据：删掉 §4 里对应的 key 就会红（见下 `undocumented`）。
    //
    // ⚠️ **这条判据的边界（如实）**：它只证「§4 那一节里**提到了这个 key**」，
    // 证不了「那段话说得对、说得全」——判一段中文说明的质量不是这里能做的。
    const layout = fs.readFileSync(
      fileURLToPath(new URL('../../../../../docs/FRONTEND-LAYOUT.md', import.meta.url)),
      'utf8',
    )
    // 只截 §4 那一节（`## 4. 信息架构骨架` → 下一个 `### 4.1`）：§4 之外的提及不算数。
    const start = layout.indexOf('## 4. 信息架构骨架')
    const end = layout.indexOf('### 4.1 ')
    expect(start, 'docs/FRONTEND-LAYOUT.md 里找不到 §4 标题').toBeGreaterThan(-1)
    expect(end, 'docs/FRONTEND-LAYOUT.md 里找不到 §4.1 标题（§4 的右边界）').toBeGreaterThan(start)
    const section4 = layout.slice(start, end)
    const undocumented = [...LAYER_ONLY_PAGE_KEYS, ...OWN_FEATURE_PAGE_KEYS].filter(
      (key) => !section4.includes(key),
    )
    expect(
      undocumented,
      `这些 key 在 docs/FRONTEND-LAYOUT.md §4 里没有说明段落：${undocumented.join(', ')}`,
    ).toEqual([])
    // 反向：两个集合本身不许空（否则上面那条会退化成恒真）。
    expect(LAYER_ONLY_PAGE_KEYS.size).toBeGreaterThan(0)
    expect(OWN_FEATURE_PAGE_KEYS.size).toBeGreaterThan(0)
  })
})
