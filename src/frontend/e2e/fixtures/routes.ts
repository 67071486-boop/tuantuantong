/**
 * **候选页面清单的唯一出处**（`04` 全站冒烟 与 `05` 职位对拍共用这一份）。
 *
 * ⛔ **本模块存在的唯一理由**：两套 spec 若各留一份「抽路由 → 拼候选清单」的实现，两份迟早漂移，
 * 而漂移的那一份不会报错——它只会**静默少测**（本仓 `E-0021`：同一事实不要两套判据）。
 * 所以抽取逻辑、显式清单、以及与 `router/index.ts` 的「集合相等」钉子全部搬到这里；
 * `04` 与 `05` 都只消费导出量，**一条断言都不放宽**。
 *
 * ⚠️ 这里 import 了两个**源码**文件（不是本目录下的夹具）：
 *  - `src/config/replicaRoutes.ts` —— 走 Playwright 自己的 TS 转译（`04` 一直在用，本仓实测可跑）；
 *  - `e2e/config.mjs` —— **纯 JS**，靠 `tsconfig.node.json` 的 `allowJs` 收进来（该文件的注释记了这条）。
 *  ⛔ 本文件的路径解析**只认 `config.mjs` 的 `FRONTEND_DIR`**（`e2e/**` 的出口都在那里），
 *    不要自己按 `import.meta.url` 再算一遍——两套算法就是本模块要消灭的那种漂移。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import type { Page } from '@playwright/test'

import { FRONTEND_DIR } from '../config.mjs'
import { MENU_GROUPS, TOP_LEVEL_ITEMS } from '../../src/config/menu'
import { REPLICA_ROUTES } from '../../src/config/replicaRoutes'

/** 从 `router/index.ts` 正则抽 `REPLICA_SKIP`（该文件 import 不了，只能这么拿）。 */
function readReplicaSkip(): Set<string> {
  const src = readFileSync(resolve(FRONTEND_DIR, 'src/router/index.ts'), 'utf8')
  const block = src.match(/export const REPLICA_SKIP = new Set\(\[([\s\S]*?)\]\)/)
  if (!block) {
    throw new Error(
      '没能从 src/router/index.ts 解析出 REPLICA_SKIP —— 该文件的形状变了。\n' +
        '这是**响亮失败**（不是静默少测）：请更新本文件的正则与期望条数。',
    )
  }
  return new Set([...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1]))
}

/**
 * 静态、但**不是 island 页**的路径（`layout: 'blank'` + 真实 Vue 视图）。
 *
 * `router/index.ts` 里这几条的组件分别是 `LoginView.vue` / `KefuConsoleView.vue` /
 * `v2/CarbonShell.vue`，不是 `LayuiIslandHost.vue`，也没有 `meta.islandPage`。本函数抽的是
 * 「island 冒烟清单」，它们不在语义内；由各自的页测试 / e2e 登录用例覆盖。
 * ⚠️ 新增一条时**显式**加进来（⛔ 不改成「凡 blank 就排除」的通用规则：那会让一条本该
 * 进冒烟清单的 island 页静默掉出去，正是本文件要防的「静默少测」）。
 *
 * ⭐ **2026-10-30（前端 v2 试验线，D-0344）+`/v2`**：炭火外壳（`v2/CarbonShell.vue`，
 * Element Plus + `.ttv2` 作用域 token，非 island 宿主），与 `/login` / `/kefu` 同类——
 * 它的子路由 `path: ''`（`v2-home`）不被本抽取器收（见 `readStaticIslandRoutes()` 对 `''` 的放行），
 * 父路径 `/v2` 在这里排除，两处是**同一个决定的两半**。
 */
// 企微客户端测试页是独立 Vue 视图，不使用 island 宿主。
const NON_ISLAND_PATHS: ReadonlySet<string> = new Set([
  '/login', '/kefu', '/v2', '/wecom-client-lab', '/visitor-chat', '/v2/internal-chat-visitors', '/v2/internal-chat-window',
])

/**
 * 从 `router/index.ts` 抽出**可直接导航的静态 island 路由**字面量（该文件 import 不了，只能正则）。
 *
 * ⛔ 排除的三类（**唯一出处**在这里；改动规则要同步下面 `EXPLICIT_ROUTES` 的注释）：
 *  - **参数化**路径（含 `:`）：`/orders/:order_no(\d{20})`、`/system-users/:id/edit` —— 不是固定路径，
 *    「一个固定字符串 = 一条可导航路由」在这里不成立；
 *  - catch-all `/:pathMatch(.*)*`（not-found）—— 不是页面；
 *  - **非 island 的静态页**（见上面 `NON_ISLAND_PATHS`）：`/login` 是登录表单，由
 *    `01-auth-guard.spec.ts` 覆盖；`/kefu`（客服精简工作台，2026-10-20）是 `layout: 'blank'`
 *    的 **Vue 3 + Framework7 Vue 视图**（`views/kefu/`）。
 *    ⚠️ 这两条的组件都不是 island 宿主，塞进「island 冒烟清单」会把 `04` 的候选页
 *    变成一条加载不出 iframe 的假用例；排除它们是**按本函数自己的定义**
 *    （「可直接导航的静态 **island** 路由」），⛔ 不是放宽「集合相等」那条判据。
 * ⚠️ 生成的那半在文件里写作 `path: r.path`（**无引号**）⇒ 本正则天然不吞它。
 * ⚠️ 本正则只认「行首（除空白外）就是 `path:`」⇒ `// path: '…'` 这类注释行不会被吞。
 * ⚠️ **嵌套子路由的 `path: ''`**（⭐ 2026-10-30，D-0344 的 `/v2` 子树：空 path = 落到父路径
 *    `/v2` 本身，不是一条独立可导航页面）⇒ 收它的字面量只会把 `/v2` 的**别名**塞进清单。
 *    `found` 的正则（`[^']+`）天然不吞空串；下面的 unmatched 自检把 `''` 显式列为已知第三形态，
 *    ⛔ 别把它当成「引号风格变了」的那类漂移。
 * ⚠️ **嵌套子路由的非空相对 path**（⭐ 2026-10-31，D-0346 的 `/v2` 子树：`path: 'my-orders'`
 *    = `/v2/my-orders`）：v2 页不是 island 宿主，塞进「island 冒烟清单」同样是假用例；
 *    相对 path 不以 `/` 开头 ⇒ 下面的 kept 过滤直接按「必须以 `/` 开头」收口（本仓
 *    router/index.ts 里全部真 island 路由都是绝对路径，这条规则只命中 v2 子树的嵌套项）。
 * ⛔ **同时钉住「没有被抽到的 `path:`」**：本正则只认单引号，而本仓没有 lint 规则
 *    钉引号风格 ⇒ 谁把某条路由改成双引号，它就**静默掉出**冒烟清单。所以下面显式枚举
 *    「每一条 `path:` 要么被正则吃到（单引号字面量）、要么是生成器的 `r.path`、
 *    要么是嵌套子路由的 `''`」，多出第四类即响亮失败。
 */
export function readStaticIslandRoutes(source?: string): string[] {
  const src = source ?? readFileSync(resolve(FRONTEND_DIR, 'src/router/index.ts'), 'utf8')
  const found = [...src.matchAll(/^\s*path: '([^']+)'/gm)].map((m) => m[1])
  const unmatched = [...src.matchAll(/^\s*path:\s*(.+?)\s*,?\s*$/gm)]
    .map((m) => m[1].trim())
    .filter((value) => !/^'[^']+'$/.test(value) && value !== 'r.path' && value !== "''")
  if (unmatched.length > 0) {
    throw new Error(
      `src/router/index.ts 里有 ${unmatched.length} 条 path: 既不是单引号字面量、也不是生成的 r.path、` +
        `也不是嵌套子路由的 ''：${JSON.stringify(unmatched)}。\n` +
        '多半是引号风格变了（本文件的正则只认单引号）⇒ 那些路由会**静默掉出**冒烟清单，' +
        '而「集合相等」只覆盖显式清单那半。\n' +
        '这是**响亮失败**（不是静默少测）：请同步本文件的正则（或改回单引号）。',
    )
  }
  const kept = found.filter(
    (p) => !p.includes(':') && p.startsWith('/') && !NON_ISLAND_PATHS.has(p),
  )
  if (kept.length === 0) {
    throw new Error(
      '没能从 src/router/index.ts 抽出任何静态 path 字面量 —— 该文件的形状变了。\n' +
        '这是**响亮失败**（不是静默少测）：请更新本文件的正则与 EXPLICIT_ROUTES。',
    )
  }
  return [...kept].sort()
}

/**
 * 非复刻的静态显式路由（逐条抄；改路由时这里要同步）。
 *
 * 排除规则见 `readStaticIslandRoutes()`。下面那条「集合相等」断言把本数组和路由表钉在一起，
 * vitest 常跑面是 `src/router/__tests__/explicit-routes.spec.ts`。
 * `EXPLICIT_ROUTES_BY_NAME` 必须与本数组逐条对应：只改一处，模块加载期自检当场抛。
 * 成员以数组本身为准。`/thirdparty/jidanpu-sync` 与 `/game-config/categories` 已不在路由表里。
 */
export const EXPLICIT_ROUTES = [
  '/',
  '/orders',
  '/orders/new',
  '/game-config/games',
  '/game-config/projects',
  '/system-users',
  '/system-users/new',
  '/system-users/login-restrict',
  '/system-users/blacklist',
  '/system-users/real-name',
  '/system-users/contract',
  '/thirdparty/xgj-shops',
  '/thirdparty/wecom-group-qr',
  '/feedback',
  // ⭐ 2026-10-08（T4 打手侧开放接口）：`router/index.ts` 新增静态 island 路由
  // `/open-platform/taker-apps`（`meta.islandPage: 'taker-apps'` + `meta.permissions` 齐全，
  // 非参数化、非 catch-all、非 `/login`）。⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 里那一条
  // **必须同一次**改（只改一处 ⇒ 本模块的加载期自检当场抛）。
  '/open-platform/taker-apps',
  // ⭐ 2026-10-16（自助注册线）：`router/index.ts` 新增静态 island 路由 `/self-registration`
  // （`meta.islandPage: 'self-registration'` + `meta.permissions` 齐全，非参数化、非 catch-all、
  // 非 `/login`），业务方 2026-10-16 需求在【系统人员】组加「自助注册」
  // （`docs/specs/2026-10-16-self-registration-links-design.md` §6）。⛔ 与下面
  // `EXPLICIT_ROUTES_BY_NAME` 里那一条**必须同一次**改（只改一处 ⇒ 本模块的加载期自检当场抛）。
  '/self-registration',
  // ⛔ 2026-10-18（E-0200 复测）补的两条漏抄（开放平台线 2026-10-04 / 10-07 加进
  // `router/index.ts` 时没同步本清单）。⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 里那两条
  // **必须同一次**改（只改一处 ⇒ 本模块的加载期自检当场抛）。
  '/system/performance',
  '/open-platform/apps',
  // ⭐ 2026-10-19（微信公众号集成线，D-0268）：`router/index.ts` 新增静态 island 路由
  // `/thirdparty/wechatmp-settings`（`meta.islandPage: 'system-wechatmp-settings'` +
  // `meta.permissions` 齐全，非参数化、非 catch-all、非 `/login`），【第三方平台】组追加
  // 「微信公众号设置」。⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 里那一条**必须同一次**改
  // （只改一处 ⇒ 本模块的加载期自检当场抛）。
  '/thirdparty/wechatmp-settings',
  // ⭐ 2026-10-19（QQ 机器人线 批次C）：`router/index.ts` 新增静态 island 路由
  // `/thirdparty/qqbot-settings`（`meta.islandPage: 'qqbot-settings'` + `meta.permissions` 齐全，
  // 非参数化、非 catch-all、非 `/login`），业务线在【第三方平台】那一组追加「QQ机器人设置」
  // （后端端点批次B 已上）⇒ 本清单**同一次**加上它（两线各 +1，18 → 20），
  // `EXPLICIT_ROUTES_BY_NAME` 里那一条也一起加（两处不同时改 ⇒ 下面那条
  // 「模块加载期自检」**当场抛**）。
  '/thirdparty/qqbot-settings',
  // ⭐ 2026-10-20（打手提现审核 + 支付宝代付线）：`router/index.ts` 新增静态 island 路由
  // `/agents/withdraw-review`（`meta.islandPage: 'agents-withdraw-review'` +
  // `meta.permissions` 齐全，非参数化、非 catch-all、非 `/login`），【财务相关】组追加
  // 「打手提现审核」（计划 `docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md` §5）。
  // ⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 里那一条**必须同一次**改（只改一处 ⇒ 本模块的
  // 「模块加载期自检」当场抛；`src/router/__tests__/explicit-routes.spec.ts` 是这条判据的
  // vitest 常跑面）。
  '/agents/withdraw-review',
  // 📌 2026-10-22 补登（2026-10-20 钱包线漏）：`/agents/wallet-adjust` 当时已落
  // `router/index.ts`，但本清单与 `EXPLICIT_ROUTES_BY_NAME` 都没同步 ⇒ 本用例自那天起是红的
  // （当时只跑了改动文件的测试没看见）。与 BY_NAME 里那条**同一次**改。
  '/agents/wallet-adjust',
  // ⭐ 2026-10-22（闲管家收款明细线）：`router/index.ts` 新增两条静态 island 路由——
  // `/agents/taker-wallet`（【财务相关 → 打手钱包】合并页，spec
  // `docs/specs/2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §8.2）与
  // `/finance/xgj-receipts`（【财务相关 → 闲管家收款明细】，同 spec §8.1）
  // （meta.islandPage + meta.permissions 齐全，非参数化、非 catch-all、非 `/login`）。
  // ⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 里那两条**必须同一次**改（只改一处 ⇒ 本模块的
  // 「模块加载期自检」当场抛；`src/router/__tests__/explicit-routes.spec.ts` 是这条判据的
  // vitest 常跑面）。
  '/agents/taker-wallet',
  '/finance/xgj-receipts',
  // ⭐ 2026-10-23（两线并入）：`router/index.ts` 新增两条静态 island 路由——
  // `/business/app-stats`（【APP与小程序 → 数据概览】）与 `/app-mp/articles`
  // （【APP与小程序 → 发布文章作品】），meta.islandPage + meta.permissions 齐全，
  // 非参数化、非 catch-all、非 `/login`。⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 同一次改
  // （只改一处 ⇒ 本模块的「模块加载期自检」当场抛）。
  '/business/app-stats',
  '/app-mp/articles',
  // ⭐ 2026-10-27（dl-D 代练通数据页线）：`router/index.ts` 新增两条静态 island 路由——
  // `/system/dailiantong-stats`（代练通按日统计）与 `/system/dailiantong-order-templates`
  // （代练通订单要求模板），meta.islandPage + meta.permissions 齐全，
  // 非参数化、非 catch-all、非 `/login`。⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 同一次改
  // （只改一处 ⇒ 本模块的「模块加载期自检」当场抛）。
  '/system/dailiantong-stats',
  '/system/dailiantong-order-templates',
  // ⭐ 2026-10-31（马上来查码线，D-0348）：`router/index.ts` 新增静态 island 路由
  // `/thirdparty/order-qr-fetch`（meta.islandPage 'system-order-qr-fetch' + meta.permissions
  // 齐全 + requiresAccountAdmin，非参数化、非 catch-all、非 `/login`），【第三方平台】组追加
  // 「马上来查码对接」（自有功能，不在对标 89 条渲染菜单里）。📌 2026-10-31 发版窗补登：
  // 该线上线时漏抄本清单（E-0200 同款漂移，正是 explicit-routes.spec 当场逮住的那类）。
  // ⛔ 与下面 `EXPLICIT_ROUTES_BY_NAME` 同一次改（只改一处 ⇒ 本模块的「模块加载期自检」当场抛）。
  '/thirdparty/order-qr-fetch',
]

/**
 * 显式那半的 **routeName → path**（与 `EXPLICIT_ROUTES` 同一份清单）。
 *
 * ⛔ **必须与 `EXPLICIT_ROUTES` 逐条对应**（同一份清单，两处写法）——下面那条自检会按条数
 * 与「值都在 `EXPLICIT_ROUTES` 里」交叉钉住；漂移即模块加载期抛错。
 * ⚠️ 为什么写死而不再抽 `router/index.ts`：`name:` 与 `path:` 在同一段里，正则抽取的规则
 *    （顺序、引号、注释）与 `04` 的 `path:` 抽取规则会各自演化 ⇒ 那正是本模块要消灭的漂移。
 *    这里写死的代价由「查不到就响亮失败」兜底（`05` 的 `pathsOfItems()`）。
 * 鸡蛋铺同步那一页已下架，别去找 `thirdparty-jidanpu-sync.ts` 和 `api/jidanpuSync.ts`。
 */
export const EXPLICIT_ROUTES_BY_NAME: Readonly<Record<string, string>> = {
  home: '/',
  orders: '/orders',
  'order-new': '/orders/new',
  'game-config-games': '/game-config/games',
  'game-config-projects': '/game-config/projects',
  'system-users': '/system-users',
  'system-user-new': '/system-users/new',
  'system-login-restrict': '/system-users/login-restrict',
  'system-blacklist': '/system-users/blacklist',
  'system-real-name': '/system-users/real-name',
  'system-contract': '/system-users/contract',
  'thirdparty-xgj-shop': '/thirdparty/xgj-shops',
  // ⭐ 2026-10-09（企微群码线 T6）：【第三方平台】那一组新增的「自动上传企微群码到订单截图」。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加，否则下面那条「模块加载期自检」
  // （按条数 + 值都在 `EXPLICIT_ROUTES` 里交叉钉）当场抛。
  'wecom-group-qr': '/thirdparty/wecom-group-qr',
  // ⭐ 2026-10-08（问题反馈线）：侧栏**最底部**的「问题反馈」。⚠️ 与 `EXPLICIT_ROUTES` **同一次**加，
  // 否则下面那条「模块加载期自检」（按条数 + 值都在 `EXPLICIT_ROUTES` 里交叉钉）当场抛。
  feedback: '/feedback',
  // ⭐ 2026-10-08（T4 打手侧开放接口）：【开放平台 → 打手对接】。⚠️ 同上，两份清单同一次加。
  'taker-apps': '/open-platform/taker-apps',
  // ⭐ 2026-10-16（自助注册线）：【系统人员 → 自助注册】。⚠️ 同上，两份清单同一次加，
  // 否则下面那条「模块加载期自检」（按条数 + 值都在 `EXPLICIT_ROUTES` 里交叉钉）当场抛。
  'self-registration': '/self-registration',
  // ⛔ 2026-10-18（E-0200 复测）：上面 `EXPLICIT_ROUTES` 同一次的三条——
  // 两条漏抄的路由按 `router/index.ts` 里的 `name:` 逐字登记（`system-perfanalysis` /
  // `open-apps`）；`game-config-categories` 那条随已删除的 `/game-config/categories`
  // **同一次删掉**（只删一处 ⇒ 本模块的加载期自检当场抛）。
  'system-perfanalysis': '/system/performance',
  'open-apps': '/open-platform/apps',
  // ⭐ 2026-10-19（微信公众号集成线，D-0268）：【第三方平台 → 微信公众号设置】。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加，否则模块加载期自检当场抛。
  'system-wechatmp-settings': '/thirdparty/wechatmp-settings',
  // ⭐ 2026-10-19（QQ 机器人线 批次C）：【第三方平台 → QQ机器人设置】。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加（两线各 +1，18 → 20），否则下面那条「模块加载期自检」
  // （按条数 + 值都在 `EXPLICIT_ROUTES` 里交叉钉）当场抛。
  'qqbot-settings': '/thirdparty/qqbot-settings',
  // ⭐ 2026-10-20（打手提现审核 + 支付宝代付线）：【财务相关 → 打手提现审核】。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加（两线合计 20 → 21），否则下面那条「模块加载期自检」
  // （按条数 + 值都在 `EXPLICIT_ROUTES` 里交叉钉）当场抛。
  'agents-withdraw-review': '/agents/withdraw-review',
  // 📌 2026-10-22 补登（2026-10-20 钱包线漏）：【财务相关 → 打手钱包管理】。⚠️ 与上面
  // `EXPLICIT_ROUTES` **同一次**补（23 → 24）。
  'agents-wallet-adjust': '/agents/wallet-adjust',
  // ⭐ 2026-10-22（闲管家收款明细线）：【财务相关 → 打手钱包】与【财务相关 → 闲管家收款明细】。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加（三条各 +1，21 → 24），否则下面那条「模块加载期自检」
  // （按条数 + 值都在 `EXPLICIT_ROUTES` 里交叉钉）当场抛。
  'agents-taker-wallet': '/agents/taker-wallet',
  'xgj-receipts': '/finance/xgj-receipts',
  // ⭐ 2026-10-23（两线并入）：【APP与小程序】组的两条。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加，否则下面的「模块加载期自检」当场抛。
  'app-stats': '/business/app-stats',
  'app-mp-articles': '/app-mp/articles',
  // ⭐ 2026-10-27（dl-D 代练通数据页线）：【发平台设置】组追加的两条自有功能页。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加，否则模块加载期自检当场抛。
  'dailiantong-stats': '/system/dailiantong-stats',
  'dailiantong-order-templates': '/system/dailiantong-order-templates',
  // ⭐ 2026-10-31（马上来查码线，D-0348）：【第三方平台 → 马上来查码对接】（发版窗补登，同上）。
  // ⚠️ 与 `EXPLICIT_ROUTES` **同一次**加，否则模块加载期自检当场抛。
  'system-order-qr-fetch': '/thirdparty/order-qr-fetch',
}

/* ——— 参数化详情页的**登记表唯一出处**（detail-pages-audit spec §4） ——— */

/**
 * 登记表里 `pattern` 的**参数写法**：`{参数名}`（与 vue-router 的 `:参数名` 区分开）。
 *
 * ⛔ 为什么不用 `:参数名` 当占位符（本文件要同时做两件事）：
 *  - **扫描**：要从 `router/index.ts` 里找出「还有没有别的参数化 path」⇒ 判据是 `path` 里含 `:`
 *    （vue-router 的语法）。若 `pattern` 也用 `:`，两件事的判据就混成一条、分不清"声明"与"现实"。
 *  - **实例化**：要从 `pattern` 拼出可导航的具体路径 ⇒ 需要一个**不跟 vue-router 语法撞车**的占位符。
 *  - 两侧能对上由**模块加载期自检**保证（见 `assertParameterizedPatternsMatchRouter()`）。
 */
const PARAM_OPEN = '{'
const PARAM_CLOSE = '}'

/** `orders.ts` 里订单号形状常量的**名字**（自检按它抽取，⛔ 不 import 那个模块，见下）。 */
const ORDER_NO_SHAPE_CONST = 'ORDER_NO_RE'

/**
 * 订单号形状（`^\d{20}$`）——**与 `src/api/orders.ts` 的 `ORDER_NO_RE` 同源，靠模块加载期自检钉住**。
 *
 * ## ⛔ 为什么不 import `isOrderNoShape`（实测过的失败，不是偷懒）
 * 试过 `import { isOrderNoShape } from '../../src/api/orders'`：那个模块 import 了 `./http`，
 * 而 `http.ts` 第 34 行是 `baseURL: import.meta.env.VITE_API_BASE ?? ''`。
 * Playwright 把 E2E 的 TypeScript 按 **Node ESM** 跑（不走 Vite 的转译）⇒ `import.meta.env` 不是
 * Vite 注入的那个对象（是 `undefined`）⇒ 访问 `.VITE_API_BASE` 直接抛，整套 E2E **一条用例都收集不到**。
 * 实测原始报错（`node e2e/run.mjs --grep "zz probe"`，探针跑完即删）：
 *
 * ```text
 * TypeError: Cannot read properties of undefined (reading 'VITE_API_BASE')
 *   at ../../src/api/http.ts:34
 * Error: No tests found
 * ```
 *
 * ⇒ 按 spec §2 的退路：**退用 `^\d{20}$` + 一条自检**。⚠️ 与别的"第二份判据"（`E-0021`）不同，
 * 这里**不是**两份会各自漂移的实现，而是**一份被抽取出来的值**：
 * 模块加载期**读 `src/api/orders.ts` 的原文**、抽出 `ORDER_NO_RE = /…/` 的字面量、
 * 与下面这个串**逐字比对**，对不上就**响亮失败**（实现见 `readOrderNoShapeSource()` 与它下面
 * 那段加载期自检）。
 * ⇒ 谁改了 `orders.ts` 的那个常量，本文件当场抛错（⛔ 不是静默少判几个实例）。
 */
const ORDER_NO_SHAPE_SOURCE = '^\\d{20}$'

/** 订单号形状判据（从 `ORDER_NO_SHAPE_SOURCE` 编译；`new RegExp` 只在这里做一次）。 */
const ORDER_NO_SHAPE_RE = new RegExp(ORDER_NO_SHAPE_SOURCE)

/**
 * 一条参数化路由**取实例**得到的形状校验（被判违规时，失败文案要把 `label` 原样打出来）。
 *
 * ⚠️ 形状**不在这里写第二遍**：`test` 是对**同一个** `ORDER_NO_SHAPE_RE` / 数字判据的引用，
 * `label` 只是它的**人读description**（`E-0021`：同一事实两套判据必然漂移，而漂移只表现为静默少判）。
 */
type ShapeRule = {
  /** 这个形状说的是什么（失败文案里要说清"期望什么"）——人读，⛔ 不参与判据。 */
  label: string
  /** 判据本体。 */
  test: (value: string) => boolean
}

/** 订单号的形状判据（形状本体与 `orders.ts` 同源，见 `ORDER_NO_SHAPE_SOURCE` 的注释）。 */
const ORDER_NO_SHAPE: ShapeRule = {
  label: `订单号形状 ${ORDER_NO_SHAPE_SOURCE}（20 位纯数字；后端 Order.order_no 的 lookup_value_regex = r"\\d{20}"）`,
  test: (value) => ORDER_NO_SHAPE_RE.test(value),
}

/** 系统账号主键的形状判据（`router` 的 `:id` 是 DRF 的整型主键；⛔ 不拿订单号那一条冒充）。 */
const SYSTEM_USER_ID_SHAPE: ShapeRule = {
  label: '正整数（DRF 整型主键；`/system-users/:id/edit` 的 `:id`）',
  test: (value) => /^\d+$/.test(value) && Number(value) > 0,
}

/**
 * 一次「取实例」的原样结果（**判据要看的是这个，不是加工过的布尔值**）。
 *
 * ⛔ 它是 spec §2「取不到实例 ⇒ 记 `noInstance`（带那次 API 的结果）」里那个"结果"：
 * 只记"没取到"而不记**为什么**，读报告的人分不清「这租户没数据」与「端点 403 / 500 / 形状不对」
 * ——那三种的处置完全不同（数据问题 / 权限问题 / 契约问题）。
 */
export type ResolveApiDetail = {
  /** 打的端点（含 query；⛔ 只记路径与查询，不带域名与凭据）。 */
  endpoint: string
  /** HTTP 状态码；网络层直接失败（超时 / 连不上）时是 `null`。 */
  httpStatus: number | null
  /** 统一信封的业务码；响应不是 JSON / 连不上时是 `null`。 */
  code: number | null
  /** 信封的 `msg`（给人读的失败原因）；取不到时是空串。 */
  msg: string
}

/**
 * 「没取到实例」的**两类**（detail-pages-audit spec §2 的两分类；评审修复波第 3 条）：
 *
 *  - `noData`：**这个职位就是没数据**（HTTP 200 / 业务码 0 / 列表是**空数组** **且**
 *    `data.total === 0`）⇒ 记 `noInstance`、**不判红**（数据短缺不是权限缺陷，覆盖率与数据相关）。
 *  - `contractBroken`：**解析契约坏了**（网络层失败 / HTTP 非 2xx / 业务码非 0 /
 *    响应缺字段（`data.items` 不是数组）/ 实例不过形状判据 /
 *    **翻页元数据 `total > 0` 但列表为空**）⇒ **必须红**。
 *
 * ⛔ **为什么必须分**：两类混成一种的后果**不对称**——把"没数据"判红会天天假红（然后没人看），
 * 把"契约坏"当"没数据"则会让**静默失效**（例如把 `resolve` 读的字段名写错、只剩另一条路由
 * 被实例化）整轮绿、只留一行 `noInstance`（评审已读码推演出这个掩盖窗口）。
 *
 * ⚠️ **加固（spec §8.9）**：最后那条（`total > 0` 但列表为空）堵的是**另一个**静默窗口——
 * 取数条件写错（页码 / 过滤条件）⇒ 端点**老老实实**回一个空列表 ⇒ 落进 `noData`、
 * **永远不红**，而网的覆盖率**静默归零**。`paginated()`（`apps/core/envelope.py`）恒填 `total`
 * ⇒ 两者矛盾只可能是取数条件坏。
 */
export type ResolveFailureKind = 'noData' | 'contractBroken'

/** 一次「取实例」的结果：**成功**（拿到实例、形状已校验）或**没取到**（带原样 API 结果）。 */
export type ResolveResult =
  | {
      ok: true
      /** 实例化后的**具体路径**（如 `/orders/20260925120105164092`）——直接进该职位的页面清单。 */
      path: string
      /** 取到的原始实例值（订单号 / 账号 id）——给人读的报告里要能看见"扫的是哪一条"。 */
      shapeValue: string
      api: ResolveApiDetail
    }
  | {
      ok: false
      /** 两类之一（见 `ResolveFailureKind`）：只有 `noData` 允许"记了不判红"。 */
      kind: ResolveFailureKind
      /** **坏在哪一步**（人读的短标签，进失败文案；`noData` 时是"列表为空"）。 */
      where: string
      /** 人读的原因（含 API 状态码 / 业务码 / 形状不符的细节；契约坏时**贴出拿到的原值**）。 */
      reason: string
      api: ResolveApiDetail | null
    }

/**
 * 一条**参数化路由**的登记（spec §4 的形状：`pattern` / `routeName` / `detailOf` / `resolve` / `shape`）。
 *
 * ⛔ **登记表是唯一出处，且必须显式枚举**：spec §4 明令
 * 「⛔ 不写『任何参数化路由都自动收』的通用规则（那会让一条没配父页的新路由静默混进来）」。
 * 所以下面那个数组只写两条，而"有没有漏登记"由**模块加载期自检**按 `router/index.ts` 的
 * 参数化 path **全集**对拍（多一条没登记的 ⇒ 当场抛错）。
 */
export type ParameterizedRoute = {
  /**
   * 带**占位符**的路径模板（spec §4 的写法）：`/orders/{order_no}` / `/system-users/{id}/edit`。
   * ⚠️ 占位符是 `{参数名}`（⛔ 不是 vue-router 的 `:参数名`，理由见 `PARAM_OPEN` 的注释）。
   */
  pattern: string
  /** `router/index.ts` 里那条参数化路由的 `name`（自检会与路由表逐字对拍）。 */
  routeName: string
  /**
   * **父列表路由**：这条详情页是"从哪个列表页点进来的"——判据②的上界靠它**推导**（spec §3）。
   * ⛔ 父页在该职位**不可达** ⇒ 这条详情页不进该职位的清单（也就不会去取实例）。
   */
  detailOf: string
  /** 怎么**用该职位自己的会话**取一个它看得见的实例（spec §2：⛔ 不许拿管理员的实例硬塞）。 */
  resolve: (page: Page) => Promise<ResolveResult>
  /** 实例必须过的**形状判据**（spec §2 的最后一颗：订单号用仓里既有的那一份，见 `ORDER_NO_SHAPE`）。 */
  shape: ShapeRule
}

/** 统一响应信封（DRF 项目的 `{code,msg,data}`；与 `fixtures/api.ts` 的 `Envelope` 同形）。 */
type Envelope<T> = { code: number; msg: string; data: T }

/**
 * 该职位自己的会话**读一个它看得见的订单**，返回 `order_no`。
 *
 * ⛔ **必须走 `page.request`**（不是 `request`）：前者带当前登录态的 cookie 罐，
 * 后者是一个**没有身份**的新 context ⇒ 拿不到"这个职位看得见的那一笔"。
 * ⛔ **不许**退回"用管理员取一笔、塞给别的职位"：谁看得见哪一笔是**数据权限**维度，
 * 塞进去会把假象当结论（spec §2 的原话）。
 */
async function resolveOrderInstance(page: Page): Promise<ResolveResult> {
  const endpoint = '/api/business/orders/?page=1&page_size=1'
  const res = await page.request.get(endpoint).catch(() => null)
  if (res === null) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '网络层失败（请求没拿到响应）',
      reason: `取实例失败：请求 ${endpoint} 没有拿到响应（网络层失败）`,
      api: null,
    }
  }
  const api: ResolveApiDetail = { endpoint, httpStatus: res.status(), code: null, msg: '' }
  if (res.status() !== 200) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: 'HTTP 非 2xx',
      reason: `取实例失败：${endpoint} 回 HTTP ${res.status()}`,
      api,
    }
  }
  let body: Envelope<{ items?: unknown; total?: unknown }>
  try {
    body = (await res.json()) as Envelope<{ items?: unknown; total?: unknown }>
  } catch {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '响应不是 JSON',
      reason: `取实例失败：${endpoint} 的响应不是 JSON`,
      api,
    }
  }
  api.code = typeof body.code === 'number' ? body.code : null
  api.msg = typeof body.msg === 'string' ? body.msg : ''
  if (body.code !== 0) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '业务码非 0',
      reason: `取实例失败：${endpoint} 的业务码是 ${String(body.code)}（msg：${api.msg || '（空）'}）`,
      api,
    }
  }
  // ⛔ 顺序是承重的：**先看契约（`data.items` 是不是数组）、再看数据（是不是空）**。
  //    `resolve()` 读的是这**同一个** `items` 表达式 ⇒ 谁把字段名写错（`items` → 别的名字），
  //    都在这一条上被判成 `contractBroken`（评审修复波第 3 条要堵的掩盖窗口）。
  const items = body.data?.items
  if (!Array.isArray(items)) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '响应缺字段（data.items 不是数组）',
      reason:
        `取实例失败：${endpoint} 的 code=0，但 data.items 不是数组（实际 ${JSON.stringify(items)}）` +
        `——响应契约坏了，或本函数读错了字段名`,
      api,
    }
  }
  if (items.length === 0) {
    // ⚠️ **加固（spec §8.9）**：翻页元数据说还有 N 条、列表却是空的 ⇒ 这**不是"没数据"**，
    //    而是**取数条件写错**（端点照办却要不到东西，例如页码写成 `page=999`、或多带一个
    //    后端会照办的过滤条件）。这种错原来会静默落进 `noData`、**永远不红**，而网的覆盖率
    //    **静默归零**（这是两分类之后剩下的最后一个静默窗口）。
    //    `EnvelopePagination`（`apps/core/envelope.py` 的 `paginated()`）**恒填** `total` ⇒
    //    `total > 0` 与"列表为空"**不可能同时**成立，除非取数条件坏。
    //    ⚠️ `total` 缺失 / 不是数字时**不在这里判**（本加固只管"元数据自相矛盾"这一形态，
    //    不把缺字段一并算进来——那是另一条判据的判别面）。
    const total = body.data?.total
    if (typeof total === 'number' && total > 0) {
      return {
        ok: false,
        kind: 'contractBroken',
        where: '翻页元数据与列表矛盾（total > 0 但 items 为空）',
        reason:
          `取实例失败：${endpoint} 的 code=0，翻页元数据说 total=${String(total)} 条，` +
          `但 data.items 是空列表——两者矛盾 ⇒ **取数条件写错**（页码 / 过滤条件写坏，` +
          `端点照办却要不到东西），⛔ 不是"这个职位没数据"`,
        api,
      }
    }
    return {
      ok: false,
      kind: 'noData',
      where: '列表为空',
      reason: `取实例失败：${endpoint} 的 code=0 且 data.items 是空列表（这个职位看不到任何订单）`,
      api,
    }
  }
  const raw = (items[0] as { order_no?: unknown }).order_no
  const value = typeof raw === 'string' ? raw : ''
  // ⛔ 形状判据是**唯一**拦口（"不是串 / 空 / 不是 20 位数字"三种都落在这里）：
  //    装配处（本函数）与声明处（`ORDER_NO_SHAPE`）用的是**同一个** `test`（`E-0021`）。
  //    ⚠️ 列表**非空**却过不了形状 ⇒ 这是**契约坏**（不是"没数据"）：`items[0]` 在、但它的
  //    `order_no` 不是合法订单号 ⇒ 必须红（原值已贴进 `reason`）。
  if (!ORDER_NO_SHAPE.test(value)) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '实例不过形状判据',
      reason:
        `取实例失败：订单号形状不符（实际 ${JSON.stringify(raw)} 类型 ${typeof raw}）` +
        `；形状判据：${ORDER_NO_SHAPE.label}——与 orders.ts 同源，见 ORDER_NO_SHAPE_SOURCE`,
      api,
    }
  }
  return { ok: true, path: `/orders/${value}`, shapeValue: value, api }
}

/**
 * 该职位自己的会话**读一个它看得见的系统账号**，返回 `id`（拼 `/system-users/<id>/edit`）。
 *
 * ⚠️ 这条路由的门是 `system-user-edit`（节点 `system_user.add_edit`）**且** `requiresAccountAdmin`
 * ⇒ **只有系统管理员能进**（spec §1 已登记"带宽窄，顺手覆盖"）。别的职位根本不会来取实例
 * （父页 `/system-users` 不在它的 `reachable` 里，见 `ParameterizedRoute.detailOf`）。
 * ⛔ 即便如此也**不写死实例**：仍然按该职位自己的会话问一次，取不到就记 `noInstance`。
 *
 * ⚠️ **本条订了 `is_active=true` ⇒「停用账号的编辑页」永远不会被驱动**（评审修复波第 11 条，
 * 已如实登记进 spec §6）：本网只取第一个**启用中**的账号，停用账号那一形态（页面上有没有差异、
 * 会不会 403）**不在判别面里**。⛔ 别把"停用账号没报错"读成"停用账号没问题"——它是**没被看**。
 * 有意不改成"不筛 `is_active`"：那会取到停用账号、把"编辑页对停用账号的表现"混进本线
 * 无关的样本里；要覆盖它应当**另开一条**判据，而不是悄悄换掉这一条。
 */
async function resolveSystemUserInstance(page: Page): Promise<ResolveResult> {
  const endpoint = '/api/accounts/?page=1&page_size=1&is_active=true'
  const res = await page.request.get(endpoint).catch(() => null)
  if (res === null) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '网络层失败（请求没拿到响应）',
      reason: `取实例失败：请求 ${endpoint} 没有拿到响应（网络层失败）`,
      api: null,
    }
  }
  const api: ResolveApiDetail = { endpoint, httpStatus: res.status(), code: null, msg: '' }
  if (res.status() !== 200) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: 'HTTP 非 2xx',
      reason: `取实例失败：${endpoint} 回 HTTP ${res.status()}`,
      api,
    }
  }
  let body: Envelope<{ items?: unknown; total?: unknown }>
  try {
    body = (await res.json()) as Envelope<{ items?: unknown; total?: unknown }>
  } catch {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '响应不是 JSON',
      reason: `取实例失败：${endpoint} 的响应不是 JSON`,
      api,
    }
  }
  api.code = typeof body.code === 'number' ? body.code : null
  api.msg = typeof body.msg === 'string' ? body.msg : ''
  if (body.code !== 0) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '业务码非 0',
      reason: `取实例失败：${endpoint} 的业务码是 ${String(body.code)}（msg：${api.msg || '（空）'}）`,
      api,
    }
  }
  // ⛔ 与订单号那条**同一顺序、同一口径**（先契约、后数据），理由见 `resolveOrderInstance`。
  const items = body.data?.items
  if (!Array.isArray(items)) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '响应缺字段（data.items 不是数组）',
      reason:
        `取实例失败：${endpoint} 的 code=0，但 data.items 不是数组（实际 ${JSON.stringify(items)}）` +
        `——响应契约坏了，或本函数读错了字段名`,
      api,
    }
  }
  if (items.length === 0) {
    // ⚠️ **加固（spec §8.9）**：与订单号那条**同一口径、同一理由**——翻页元数据说还有 N 条、
    //    列表却是空的 ⇒ **取数条件写错**，不是"这个职位没数据"；`paginated()` 恒填 `total`。
    //    （`total` 缺失 / 不是数字时不在这里判，与上面那条一致。）
    const total = body.data?.total
    if (typeof total === 'number' && total > 0) {
      return {
        ok: false,
        kind: 'contractBroken',
        where: '翻页元数据与列表矛盾（total > 0 但 items 为空）',
        reason:
          `取实例失败：${endpoint} 的 code=0，翻页元数据说 total=${String(total)} 条，` +
          `但 data.items 是空列表——两者矛盾 ⇒ **取数条件写错**（页码 / 过滤条件写坏，` +
          `端点照办却要不到东西），⛔ 不是"这个职位没数据"`,
        api,
      }
    }
    return {
      ok: false,
      kind: 'noData',
      where: '列表为空',
      reason:
        `取实例失败：${endpoint} 的 code=0 且 data.items 是空列表（这个职位看不到任何启用中的账号）`,
      api,
    }
  }
  const raw = (items[0] as { id?: unknown }).id
  const value = typeof raw === 'number' && Number.isInteger(raw) ? String(raw) : ''
  // ⛔ 与订单号那条**同一形状**：形状判据是唯一拦口，装配处与声明处共用同一个 `test`。
  //    列表非空却过不了形状 ⇒ 契约坏（不是"没数据"）。
  if (!SYSTEM_USER_ID_SHAPE.test(value)) {
    return {
      ok: false,
      kind: 'contractBroken',
      where: '实例不过形状判据',
      reason:
        `取实例失败：账号 id 形状不符（实际 ${JSON.stringify(raw)} 类型 ${typeof raw}）` +
        `；形状判据：${SYSTEM_USER_ID_SHAPE.label}`,
      api,
    }
  }
  return { ok: true, path: `/system-users/${value}/edit`, shapeValue: value, api }
}

/**
 * **参数化详情页登记表**（spec §4）——⛔ **显式枚举两条，一条都不许"自动收"**。
 *
 * ⚠️ 为什么必须是显式清单（spec §4 的原话 + 本仓的 `E-0021`）：参数化路由**取不到父页**
 * 就无从判断"这条对某职位该不该扫"；写一条"凡是含 `:` 就自动收"的规则，会让一条**没配
 * `detailOf`** 的新路由静默混进清单——那正是本网要防的"静默多收 / 静默少测"。
 * ⇒ 漏登记的形态由**模块加载期自检**响亮拦下（见 `assertParameterizedPatternsMatchRouter()`）。
 */
export const PARAMETERIZED_ROUTES: readonly ParameterizedRoute[] = [
  {
    pattern: '/orders/{order_no}',
    routeName: 'order-detail',
    detailOf: '/orders',
    resolve: resolveOrderInstance,
    shape: ORDER_NO_SHAPE,
  },
  {
    pattern: '/v2/orders/{order_no}',
    routeName: 'v2-order-detail-preview',
    detailOf: '/orders',
    resolve: resolveOrderInstance,
    shape: ORDER_NO_SHAPE,
  },
  {
    pattern: '/system-users/{id}/edit',
    routeName: 'system-user-edit',
    detailOf: '/system-users',
    resolve: resolveSystemUserInstance,
    shape: SYSTEM_USER_ID_SHAPE,
  },
]

/**
 * 一条登记里的**参数名**（按出现顺序）——`{order_no}` ⇒ `['order_no']`。
 * ⛔ 与 `readParameterizedPaths()` 的判据（`router` 的 `:`）分开，见 `PARAM_OPEN` 的注释。
 */
export function patternParamNames(pattern: string): string[] {
  return [...pattern.matchAll(/\{([^}]+)\}/g)].map((m) => m[1])
}

/**
 * 把登记里的 `pattern` 换成**能匹配 `router/index.ts` 那条 path 的正则**。
 *
 * 逐段处理：
 *  - 模板里的 `{名}` ⇒ `([^/]+)`（**捕获**，用来核对"占位符数 = 路由参数数"）；
 *  - 路由里的 `:名` 后若紧跟 `(约束)`（vue-router 的可选正则约束，本仓两条都没有，但语法支持）
 *    ⇒ 把约束当**非捕获组** `(?:约束)?` 接上；
 *  - 其余字符（含 `\` 与 `.` 这类正则元字符）一律转义。
 */
function routerPathPattern(pattern: string): RegExp {
  const parts: string[] = []
  let i = 0
  while (i < pattern.length) {
    if (pattern[i] === PARAM_OPEN) {
      const end = pattern.indexOf(PARAM_CLOSE, i)
      parts.push('([^/]+)')
      i = end + 1
      continue
    }
    if (pattern[i] === ':') {
      let j = i + 1
      while (j < pattern.length && /[A-Za-z0-9_]/.test(pattern[j])) j++
      parts.push('[^/]+')
      i = j
      if (pattern[i] === '(') {
        // vue-router 的 `:参数(约束)` —— 约束里的内容**原样**接上（那是正则）。
        let depth = 0
        let k = i
        for (; k < pattern.length; k++) {
          if (pattern[k] === '(') depth++
          else if (pattern[k] === ')') {
            depth--
            if (depth === 0) break
          }
        }
        parts.push(`(?:${pattern.slice(i, k + 1)})?`)
        i = k + 1
      }
      continue
    }
    parts.push(escapeRegExpChar(pattern[i]))
    i += 1
  }
  return new RegExp(`^${parts.join('')}$`)
}

/**
 * 把一个字面量字符转义成"在正则里只匹配它自己"。
 *
 * ⛔ **用 `includes()` 而不是写一个字符类**：转义用的那个字符类字面量是正则语法里最难写对的
 * 一处（`]` 与 `\` 的转义互相影响）——本文件第一次就是在这里写坏了，症状是**整个模块解析失败**
 * （Playwright 报 `SyntaxError: … Unexpected token`，而且报错行**指到几十行之后**去，
 * 看起来像别处的问题）。逐字符判既读得懂，也没有那类坑。
 */
function escapeRegExpChar(ch: string): string {
  return '.*+?^$()[]{}|\\'.includes(ch) ? '\\' + ch : ch
}

/**
 * 从 `router/index.ts` 抽出**全部参数化 path**（含 `:` 的 `path:` 字面量）与它们的 `name`。
 *
 * ⛔ **这是登记表自检的"现实"那一侧**（与 `readStaticIslandRoutes()` 的排除规则**同源**：
 * 那边排除含 `:` 的，这边正是要收它们）。判据是**结构**（`path` 里含 `:`），
 * ⛔ 不是"某个已知的两条字符串"——写死就检不出"**新加了一条参数化路由却忘了登记**"。
 *
 * ⚠️ **`/:pathMatch(.*)*`（catch-all / not-found）必须排除**：它**也含 `:`**，但它是四条
 * `path:` 里的第三类（`readStaticIslandRoutes()` 的注释已登记过这一类）——**不是详情页**，
 * 没有"父列表页"可配，也不该被实例化。判据用 `*`（它与两条真详情页的结构差异就在这个通配符），
 * ⛔ 不去写它的字面量（写死就检不出"路由表把 catch-all 换成别的形状"）。
 */
function readParameterizedRoutes(): { path: string; name: string }[] {
  const src = readFileSync(resolve(FRONTEND_DIR, 'src/router/index.ts'), 'utf8')
  // ⛔ 全部 `path: '<字面量>'`（与 `readStaticIslandRoutes()` 同一条正则，只是那边随后滤掉参数化的）。
  //    用**行首锚定 + 单引号**：⛔ 不吞注释行、也不吞生成器写的 `path: r.path`（无引号）。
  const literalPaths = [...src.matchAll(/^\s*path: '([^']+)'/gm)].map((m) => m[1])
  const paramPaths = literalPaths.filter((p) => p.includes(':') && !p.includes('*'))
  // 逐条 `{ path: '…', … name: '…' }` 地找：先定位 `path:`，再在同一段里找最近的 `name:`。
  const re = /^\s*path: '([^']+)'\s*,\s*\n\s*name: '([^']+)'/gm
  const out: { path: string; name: string }[] = []
  for (const m of src.matchAll(re)) {
    if (m[1].includes(':') && !m[1].includes('*')) out.push({ path: m[1], name: m[2] })
  }
  if (paramPaths.length !== out.length) {
    throw new Error(
      `src/router/index.ts 里有 ${paramPaths.length} 条含 ':' 的 path 字面量，但只有 ${out.length} 条能同时读出 ` +
        `紧跟其后的 name: ⇒ 本文件的正则形状过时了（例如 path/name 之间插了别的键）。\n` +
        '这是**响亮失败**（不是静默少一条）：请同步本函数的正则。',
    )
  }
  return out
}

/** `router/index.ts` 的全部参数化路由（自检的"现实"侧；导出给结构钉子用）。 */
export const ROUTER_PARAMETERIZED_ROUTES: { path: string; name: string }[] = (() => {
  const all = readParameterizedRoutes()
  if (all.length === 0) {
    throw new Error(
      '没能从 src/router/index.ts 抽出任何参数化 path —— 该文件的形状变了（本仓至少应有两条）。\n' +
        '这是**响亮失败**（不是静默少一条）：请更新本文件的正则。',
    )
  }
  return all
})()

/**
 * 从 `src/api/orders.ts` 抽出订单号形状常量的**字面量**（`ORDER_NO_RE = /^\d{20}$/` ⇒ `^\\d{20}$`）。
 *
 * ⛔ 用途与理由见 `ORDER_NO_SHAPE_SOURCE`：那里为什么不能 import、以及这条自检挡住的是哪种漂移。
 * ⚠️ 抽取失败**也抛**：那说明 `orders.ts` 的这个常量被改名 / 改写法了 ⇒ 本文件的形状判据
 * 已经**不再与它同源**，宁可响亮失败，也不要把"我以为还对得上"留在代码里。
 */
function readOrderNoShapeSource(): string {
  const src = readFileSync(resolve(FRONTEND_DIR, 'src/api/orders.ts'), 'utf8')
  const m = src.match(new RegExp(`const\\s+${ORDER_NO_SHAPE_CONST}\\s*=\\s*/(.+)/\\s*$`, 'm'))
  if (!m) {
    throw new Error(
      `没能从 src/api/orders.ts 抽出 \`${ORDER_NO_SHAPE_CONST}\` 的正则字面量 —— 那个常量被改名 / ` +
        '改写法了。\n' +
        '这是**响亮失败**（不是静默少判几个实例）：本文件的订单号形状判据与它**同源**，' +
        '抽不出来就说明它们已经可能不一致了，请同步 `ORDER_NO_SHAPE_SOURCE` 与这个正则。',
    )
  }
  return m[1]
}

/**
 * 模块加载期自检①（spec §4 后半句）：**登记表必须与 `router/index.ts` 的参数化路由对上**。
 *
 * ## 判据形状（⚠️ 订正措辞，评审修复波第 10② 条）
 * 原来这里写"**双向集合相等**"——**过度**：`unmatched` 只判"每条 declared **至少**匹配一条 actual"
 * （是**覆盖**，不是相等；今天恰好一对一，但一条 declared 若变得能匹配两条 actual，原判据不会红）。
 * 现在补一条：**每条 declared 必须恰好匹配一条 actual**（多条 ⇒ 当场抛）⇒ 两个方向都是**相等**语义了。
 *  - declared 匹配不到 actual ⇒ 一条**死链**（点进去 404）/ pattern 写错，当场抛；
 *  - actual 没有 declared 匹配它 ⇒ 那条详情页**永远不会被扫**（本线要防的"静默少测"），当场抛。
 * ⚠️ 「现实」那一侧**有意排除**含 `*` 的参数化 path（`/:pathMatch(.*)*` catch-all —— **不是详情页**，
 * 见 `readParameterizedRoutes()`）⇒ 本判据的"相等"是「**排除 catch-all 之后**的集合相等」，
 * ⛔ 不是"路由表里每一条 `path:` 都被登记了"。catch-all 换成别的形状时由 `readParameterizedRoutes()`
 * 的条数对拍拦下，不在本条。
 * ⚠️ 顺带核 `name`：`pattern` 对上了、`routeName` 写错，会让报告里的归属指到别的路由上去。
 */
function assertParameterizedPatternsMatchRouter(): void {
  const actual = ROUTER_PARAMETERIZED_ROUTES
  const actualPaths = actual.map((r) => r.path)
  const declared = PARAMETERIZED_ROUTES.map((r) => r.pattern)
  // ⛔ 「每条 declared 恰好匹配一条 actual」：把"覆盖"收紧成"相等"（一条 pattern 匹配多条 ⇒ 歧义，
  //    归属会指错、实例化时也可能拼出不是这一条路由的路径）⇒ 响亮失败。
  const ambiguous = declared
    .map((p) => ({ p, hits: actualPaths.filter((a) => routerPathPattern(p).test(a)) }))
    .filter((x) => x.hits.length !== 1)
  const unmatched = ambiguous.map((x) => x.p)
  const unregistered = actualPaths.filter((a) => !declared.some((p) => routerPathPattern(p).test(a)))
  if (unmatched.length > 0 || unregistered.length > 0) {
    throw new Error(
      '参数化详情页登记表与 src/router/index.ts 对不上（这是**响亮失败**，不是静默少测）：\n' +
        (unmatched.length
          ? `  · 登记了但路由表里**恰好一条**都对不上（死链 / pattern 写错 / 一条 pattern 匹配到多条）：` +
            `${ambiguous.map((x) => `${x.p}→[${x.hits.join('、') || '（无）'}]`).join('、')}\n`
          : '') +
        (unregistered.length
          ? `  · 路由表里有但**没登记**（这条详情页永远不会被扫）：${unregistered.join('、')}\n`
          : '') +
        `  登记表：${JSON.stringify(declared)}；路由表里的参数化 path：${JSON.stringify(actualPaths)}`,
    )
  }
  for (const r of PARAMETERIZED_ROUTES) {
    const hit = actual.find((a) => routerPathPattern(r.pattern).test(a.path))
    if (hit === undefined) continue // 上面已经抛了，这里只为类型收窄。
    if (hit.name !== r.routeName) {
      throw new Error(
        `登记表里的 routeName 与路由表不符：pattern=${r.pattern} 在 src/router/index.ts 里是 ` +
          `name=${hit.name}，登记表写的是 ${r.routeName}。\n` +
          '这是**响亮失败**：routeName 决定报告里的归属，写错会把发现指到别的路由上去。',
      )
    }
    const declaredCount = patternParamNames(r.pattern).length
    const actualCount = [...hit.path.matchAll(/:[A-Za-z0-9_]+/g)].length
    if (declaredCount !== actualCount) {
      throw new Error(
        `登记表 pattern=${r.pattern} 有 ${declaredCount} 个占位符，而路由表里的 ${hit.path} 有 ` +
          `${actualCount} 个参数 ⇒ 实例化时拼不出那条路径。\n这是**响亮失败**，请同步 pattern。`,
      )
    }
    // ⚠️ **防御性写法，⛔ 不是承重判据**（评审修复波第 10① 条；`AGENTS.md` §5E）：
    //    这条只判 `detailOf` 自己"长得像不像一条绝对路径"（`/` 开头）——它是**自指**的
    //    （没跟任何外部集合对拍），去掉它不会有任何用例变红。**真正承重**的跨源判据是
    //    **收集期钉子②**：`E2E` 的 `职位对拍结构兜底：参数化详情页登记表与下界登记自洽` 里的
    //    `CANDIDATES.includes(r.detailOf)`（那条打在**候选页面清单**这个外部集合上）。
    //    保留此条只为在加载期早一点拦住明显笔误；⛔ 别在文档里宣称它有判别力。
    if (typeof r.detailOf !== 'string' || !r.detailOf.startsWith('/')) {
      throw new Error(
        `登记表 ${r.pattern} 的 detailOf 不是页面路径（以 / 开头）：${JSON.stringify(r.detailOf)}。\n` +
          '⚠️ 这是**防御性检查**：真正的跨源判据是收集期钉子的 `CANDIDATES.includes(detailOf)`。',
      )
    }
  }
}

/** 跑自检①②（模块加载期；`CANDIDATES` 等导出量在同一次加载里随后生成）。 */
assertParameterizedPatternsMatchRouter()
{
  const source = readOrderNoShapeSource()
  if (source !== ORDER_NO_SHAPE_SOURCE) {
    throw new Error(
      `订单号形状判据与 src/api/orders.ts 的 \`${ORDER_NO_SHAPE_CONST}\` **不再同源**：\n` +
        `  · 本文件（e2e/fixtures/routes.ts::ORDER_NO_SHAPE_SOURCE）：${JSON.stringify(ORDER_NO_SHAPE_SOURCE)}\n` +
        `  · src/api/orders.ts 里的字面量：${JSON.stringify(source)}\n` +
        '这是**响亮失败**（不是静默少判几个实例）：请同步 `ORDER_NO_SHAPE_SOURCE`。\n' +
        '⚠️ 为什么是"抽出来比"而不是 import：见 `ORDER_NO_SHAPE_SOURCE` 上方那段实测（真实浏览器跑不起来）。',
    )
  }
}

/** `router/index.ts` 里「对标有、我们不做」的登记集合条数（`04` 的结构兜底断言钉它）。 */
export const REPLICA_SKIP: Set<string> = readReplicaSkip()

/** `router/index.ts` 里抽出的静态 island 路由（已排序）；与 `EXPLICIT_ROUTES` 集合相等。 */
export const STATIC_ISLAND_ROUTES: string[] = readStaticIslandRoutes()

/** 由 `replicaRoutes.ts` 生成的那一半（`04` 与 `05` 都按它算条数）。 */
export const GENERATED_ROUTES: string[] = REPLICA_ROUTES.filter(
  (r) => !r.accessDenied && !REPLICA_SKIP.has(r.key),
).map((r) => r.path)

/**
 * **routeName → path**（全部**可导航**的候选页）：生成的那半（`replicaRoutes.ts` 的
 * `routeName` / `path` 成对，已排除 `accessDenied` 与 `REPLICA_SKIP`）+ 显式那半。
 *
 * ⚠️ 这是给 `05` 把「DOM 里的菜单标签」翻译成路径用的，⛔ 它**不是**可见性的判据：
 *    可见性读 DOM，这里只做翻译（`AGENTS.md` §5C）。
 * ⚠️ 只收 `CANDIDATES` 里的路径：`replicaRoutes.ts` 里还有几条**参数化** path
 *    （含 `:` 的详情页）不是可导航的固定路径，本表有意不收——它们本来也不该出现在菜单里。
 */
export const ROUTE_PATH_BY_NAME: Map<string, string> = (() => {
  const map = new Map<string, string>()
  for (const [name, path] of Object.entries(EXPLICIT_ROUTES_BY_NAME)) map.set(name, path)
  for (const r of REPLICA_ROUTES) {
    if (r.accessDenied || REPLICA_SKIP.has(r.key)) continue
    map.set(r.routeName, r.path)
  }
  return map
})()

/**
 * 「**有意隐藏**」的菜单条目所对应的路径集合 —— `05` 判据②（`onlyReachable(R) ⊆ 本集合`）的上界。
 *
 * 推导规则的**唯一出处是 `src/config/menu.ts`**（⛔ 不是 `05` 里的常量，更不是「T2 实测报出来的那 21 条」）：
 *  - **分组级** `hidden: true` ⇒ 该组**全部**条目的路径（今天：游戏号库 / 客户维护 / 游戏排团）；
 *  - **条目级** `hidden: true` ⇒ 该条目的路径（今天：顶层「我参与的排团」，以及
 *    ⭐ 2026-10-26 T5 起【发平台设置】组里本批未放出的 4 条——该组本身已改为可见；那 4 条
 *    或已有其它入口（如「自动发消息到微信群设置」在【系统维护】可见）、或页面尚未接后端）。
 *
 * ⛔ **为什么必须推导、不许写死**：写死就失去本条的全部判别力——本条今天要能区分
 * 「有意隐藏」（冻结 IA：入口不上菜单、但页面仍接线，见 `menu.ts:99` / `menu.ts:135` 的注释）
 * 与「**新接了路由却忘了加菜单入口**」。前者允许出现在 `onlyReachable` 里，后者一旦出现必须红。
 *
 * ⚠️ **自证不是空转**：本集合若因推导失败变成**空集**，`admin` 那 21 条实测的 `onlyReachable`
 * 会立刻违反「⊆ 本集合」⇒ 当场红。⇒ 不需要再写一条「本集合非空」的自指断言（`E-0024`）。
 *
 * ⚠️ 置灰条目（`routeName === undefined`，如【系统维护】的「自动发消息到微信群设置」）**不进本集合**：
 * 它没有可去的路径，候选清单（`CANDIDATES`）里也没有它 ⇒ 两侧都不计才是同一把尺（`E-0083`）。
 * 反之，**有 `routeName` 却在路由表里查不到 path** 会当场响亮失败——那已经是死链，不是「隐藏」。
 */
export const HIDDEN_MENU_PATHS: Set<string> = (() => {
  const out = new Set<string>()
  const push = (routeName: string | undefined, where: string): void => {
    if (routeName === undefined) return
    const path = ROUTE_PATH_BY_NAME.get(routeName)
    if (path === undefined) {
      throw new Error(
        `menu.ts 里隐藏的条目「${where}」指向 routeName=${routeName}，但路由表里没有它的 path。\n` +
          '「有意隐藏」的前提是**页面仍接线**。查不到路径 = 这条已经是死链，' +
          '本模块拒绝把它当成「隐藏」（这是**响亮失败**，不是静默少一条）。',
      )
    }
    out.add(path)
  }
  for (const item of TOP_LEVEL_ITEMS) {
    if (item.hidden) push(item.routeName, item.label)
  }
  for (const group of MENU_GROUPS) {
    for (const item of group.items) {
      if (group.hidden || item.hidden) push(item.routeName, `${group.name} / ${item.label}`)
    }
  }
  return out
})()

/**
 * 模块加载期自检：`EXPLICIT_ROUTES_BY_NAME` 与 `EXPLICIT_ROUTES` 必须是**同一份清单**
 * （前者少一条 ⇒ `05` 的菜单翻译会响亮失败；多一条 ⇒ 后者会少测一页）。⛔ 不许静默。
 */
{
  const values = Object.values(EXPLICIT_ROUTES_BY_NAME)
  if (values.length !== EXPLICIT_ROUTES.length) {
    throw new Error(
      `EXPLICIT_ROUTES_BY_NAME 有 ${values.length} 条，EXPLICIT_ROUTES 有 ${EXPLICIT_ROUTES.length} 条` +
        '——两份显式清单漂移了（这是**响亮失败**，不是静默少测）。',
    )
  }
  const missing = values.filter((p) => !EXPLICIT_ROUTES.includes(p))
  if (missing.length > 0) {
    throw new Error(
      `EXPLICIT_ROUTES_BY_NAME 里有 EXPLICIT_ROUTES 里没有的路径：${missing.join('、')}。`,
    )
  }
}

/**
 * 候选页面清单：生成的 + 显式的，去重后排序。
 * ⚠️ 顺序**必须是排序后的**：`04` 的体检表行文件按它遍历、`05` 的 `reachable` 也按它导航，
 * 两处用同一份顺序 ⇒ 失败时的行号 / 报告顺序可对齐。
 */
export const CANDIDATES: string[] = [...new Set([...GENERATED_ROUTES, ...EXPLICIT_ROUTES])].sort()

/**
 * 等这次导航的**终态**落定，返回终态 `pathname`。
 *
 * ⛔ **唯一出处**：`05`（T2）现在用它，`T3` 驱动控件后读终态也要用它——⛔ 不要各写一份
 * （`E-0021`：同一事实两套判据必然漂移，而漂移只表现为静默少测）。
 *
 * ⛔ **不能 `goto()` 之后立刻读 `page.url()`**（T2 执行期实测踩到）：`goto` 等的是 `load`，
 * 而路由守卫是**异步**的（`guards.ts` 先 `await session.load()` 再决定弹不弹回 `/`）
 * ⇒ 被拒的路径在 `load` 那一刻 URL 还是**原路径**，几毫秒后才变 `/`。
 * 实测（cs 走 `/customers`）：`load=305ms`，URL 在 **`load` 之后 5ms** 才变成 `/`；
 * 而 `page.url()` 在 `goto` 返回的那一瞬读到的是 `/customers` ⇒ **被拒的页面会被算成放行**
 * （首轮实测：cs 被算成 60/60 全放行，与 spec §1 的「48 条弹回」直接矛盾——那是探针的错觉）。
 *
 * 判据 = **path 连续两次采样相同**：守卫的跳转是 `router.replace`，落在同一个文档里，
 * 变化只可能发生在最开始几十毫秒内；稳定即终态。⛔ **不用 `waitForLoadState('networkidle')`**：
 * 实测它对被拒页要多花约 600ms（整轮 5 个职位 × 60 条 ⇒ 多好几分钟），而且它等的是网络静默、
 * 不是「守卫决定了」。
 */
export async function settledPath(page: Page): Promise<string> {
  // ⚠️ 传**函数体**（不是 `() => boolean` 的普通回调）：Playwright 只有在传函数体时才把它
  //    放进页面上下文执行、从而**能 `await`**（普通回调被序列化后是同步的，写 `await` 会恒返回
  //    Promise 对象 ⇒ 恒真）。`waitForFunction` 的轮询默认走 `requestAnimationFrame`，够快。
  await page
    .waitForFunction(
      async () => {
        const first = window.location.pathname
        await new Promise((resolve) => setTimeout(resolve, 40))
        return window.location.pathname === first
      },
      undefined,
      { timeout: 5_000 },
    )
    .catch(() => {
      /* 超时就用当前值：读数交给调用方，⛔ 这里不把「没稳定」伪装成「稳定」。 */
    })
  return new URL(page.url()).pathname
}
