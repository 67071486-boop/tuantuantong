// @vitest-environment jsdom
// 必需：本文件 `import realRouter from '@/router'`，而它用 `createWebHistory`（解构 window）。
import fs from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import realRouter from '@/router'
import { PUBLIC_ROUTE_NAMES, ROUTE_NODE_CODES, nodeCodesFor } from '../routePermissions'

/**
 * 从后端目录（`src/apps/rbac/catalog.py`）抽出**全部合法节点 code**。
 *
 * 为什么前端测试读后端文件：这是唯一能抓「前端把 code 打错一个字母」的东西——
 * 打错的后果是**静默**的（该页对所有非管理员消失），`pnpm lint`/`typecheck` 都看不见。
 * 只读、不改；`catalog.py` 的形状由后端 `tests/test_rbac_catalog.py` 钉住，可稳定解析。
 *
 * 自证下界：抽不到 165 个就红（防止解析规则失效后本文件真空绿）。
 * ⚠️ 2026-09-23（本线裁剪 43 点 + 105 改造）：目录由「13 栏目 + 197 功能点 = 210」变为
 * 「13 + 154 = **167**」⇒ 这条字面量下界同步为 167（判据强度不变：仍是**恰好相等**）。
 * ⚠️ 2026-09-23（`feat/order-recognition-and-cleanup`）：4 个已下线系统设置页的权限点从
 * `catalog.py` 删除 ⇒ 「13 + 150 = **163**」，故本行同步为 163（仍是**恰好相等**）。
 * ⭐ 2026-10-08（T4 打手侧开放接口）：新增本仓第一条**自有节点**
 * `system.allow_taker_open_api` ⇒ 「13 + 151 = **164**」，故本行同步为 164（仍是**恰好相等**）。
 * ⭐ 系统开关线（D-0223）：第二条自有节点 `system.edit_settings`
 *   ⇒ 「13 + 152 = **165**」，本行同步为 165。
 * ⭐ QQ 机器人线（批次B）：第三条自有节点 `system.qqbot_settings`
 *   ⇒ 「13 + 153 = **166**」，本行同步为 166。
 * ⭐ 企微自建应用线（D-0379，2026-11-11）：第十一条自有节点 `system.wecom_settings`
 *   ⇒ 「13 + 153 = **166**」（客户维护组收敛后 152 点 +1）。
 */
function catalogCodes(): Set<string> {
  // ⚠️ 这里用 `dirname(fileURLToPath(import.meta.url))` + `resolve`，**不是**字面量首参
  // `new URL('<字面量>', import.meta.url)`：`catalog.py` 在 Vite root（`src/frontend`）**之外**，
  // 字面量首参会被静态改写成 `http://localhost:3000/@fs/...` ⇒ `fileURLToPath` 抛
  // `TypeError: The URL must be of scheme file`（实测）。
  // 本仓另有读盘用例用的是字面量首参（`config/__tests__/menuIcons.spec.ts`、
  // `config/__tests__/replicaRoutes.spec.ts`）——它们的**目标在 root 之内**，所以那条路可用；
  // 判别因素是"目标在不在 root 内"，不是"哪个写法更对"。
  const here = dirname(fileURLToPath(import.meta.url))
  const file = resolve(here, '../../../../apps/rbac/catalog.py')
  const src = fs.readFileSync(file, 'utf8')
  const codes = new Set<string>()
  // MODULES：("my_orders", 1, "我的订单", "…", "/Order/MyOrderList")
  for (const m of src.matchAll(/^\s*\("([a-z][a-z0-9_]*)",\s*\d+,\s*"/gm)) codes.add(m[1]!)
  // NODES：("order.list", 14, "订单管理列表", "…", "/Order/OrderList", NodeKind.PAGE, False)
  // ⭐⭐ **T4改准：`benchmark_id` 那一格要允许 `None`**。
  // 原先这里写死 `\d+`，而**自有节点**（本仓第一条就是 `system.allow_taker_open_api`）的
  // `benchmark_id` 是 `None` ⇒ 那一行**匹配不上** ⇒ `catalogCodes()` 少一条
  // ⇒ 下面那条「表里的每个 code 都真实存在于后端目录」会把 `taker-apps` 判成**打错字**
  // （而它其实是对的）⇒ 症状是**误导性**的假红。
  // ⛔ 所以这条正则必须与"目录里可以出现 `None`"这件事**同一次**改；
  // ⛔ 只改本函数**上方**那条计数（163 → 164）**不够**——那只会让"少一条"这件事换个地方红。
  // ⚠️ `(?:None|\d+)` 而不是 `.*`：仍然只认这两种形态（多了第三种形状要显式加）。
  for (const m of src.matchAll(/^\s*\("([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)",\s*(?:None|\d+),/gm)) {
    codes.add(m[1]!)
  }
  return codes
}

describe('路由 → 节点 code 表', () => {
  // ⭐ 本表实测 **165**：自有节点（`benchmark_id = None`）与目录裁剪逐批登记出来的合计——
  //    九条早期自有节点（`home.qr_manage` / `system.qqbot_settings` /
  //    `finance.taker_withdraw_review` / `finance.taker_wallet_adjust` /
  //    `finance.xgj_receipt_detail` / `system.article_manage` / `order.platform_write` 等）+
  //    2026-11-11 客户维护组收敛删 8 点（迁移 0015）+ 同批新增自有限制节点
  //    `customer.member_list_self_only`（迁移 0016）（各自落点见 `routePermissions.ts`）。
  //    真值由本网自己报出，⛔ 不按「上一档 + 1」推算；新增自有节点时同一次更新这里与后端钉。
  it('解析后端目录得 170 个节点（自证下界；解析规则失效时这里会红）', () => {
    expect(catalogCodes().size).toBe(170)
  })

  it('表里的每个 code 都真实存在于后端目录（打错一个字母就红）', () => {
    const known = catalogCodes()
    // ⚠️ D-0219 起值可以是数组（多节点任一）：先归一化再查（⛔ 不是跳过数组——
    // 数组里打错字母同样要红）。
    const unknown: string[] = []
    for (const [name, value] of Object.entries(ROUTE_NODE_CODES)) {
      for (const code of Array.isArray(value) ? [...value] : [value]) {
        if (!known.has(code)) unknown.push(`${name} → ${code}`)
      }
    }
    expect(unknown).toEqual([])
  })

  it('企微客户端测试页使用企微设置权限', () => {
    expect(ROUTE_NODE_CODES['wecom-client-lab']).toBe('system.wecom_settings')
    expect(realRouter.getRoutes().find((route) => route.name === 'wecom-client-lab')?.meta.permissions).toEqual(['system.wecom_settings'])
  })

  it('承重：白名单外**每条**真路由都在表里，且表里**没有**多余条目', () => {
    const routed = realRouter
      .getRoutes()
      .map((r) => String(r.name ?? ''))
      .filter((n) => n !== '' && !PUBLIC_ROUTE_NAMES.has(n))
      .filter((n) => !(import.meta.env.DEV && n === 'v2-internal-chat'))
      .sort()
    expect(Object.keys(ROUTE_NODE_CODES).sort()).toEqual(routed)
  })

  it('白名单覆盖无需业务节点的路由', () => {
    expect([...PUBLIC_ROUTE_NAMES].sort()).toEqual(['home', 'login', 'not-found', 'v2', 'v2-approvals', 'v2-home', 'v2-internal-chat-settings', 'v2-internal-chat-visitors', 'v2-internal-chat-window', 'v2-order-detail-preview', 'v2-wecom-archive', 'v2-wxpusher-settings', 'visitor-chat'])
  })

  it('nodeCodesFor：命中回单元素数组、数组值原样展开、未命中回 []（不抛）', () => {
    expect(nodeCodesFor('orders')).toEqual(['order.list'])
    // D-0219 起的「多节点任一」形态：数组值按序展开（消费方 `canNode` 是 some 语义）。
    expect(nodeCodesFor('agents-performance')).toEqual(['finance.payroll', 'order.list'])
    expect(nodeCodesFor('nope')).toEqual([])
  })

  it('每个 code 的形态合法：栏目码无点号，功能点码恰好一个点号（数组值逐个查）', () => {
    for (const value of Object.values(ROUTE_NODE_CODES)) {
      for (const code of Array.isArray(value) ? [...value] : [value]) {
        expect(code).toMatch(/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$/)
      }
    }
  })

  it('【角色职位权限】这条落在 ①线唯一翻真的那 4 个点上（brief 第 4 节 / spec §11 第 12 行）', () => {
    expect(ROUTE_NODE_CODES['system-role-authority']).toBe('system.role_authority')
  })

  it('2026-10-17：「经营报表」与「客服接发单统计」共用 finance.support_statistics（复用节点，⛔ 不新增）', () => {
    // spec `2026-10-17-support-stats-and-business-report-design.md` §0 第 3 条裁定：
    // 经营报表放【财务相关】组、**复用**客服接单统计那个节点（不新增权限点、不动 150 条冻结清单）。
    // ⚠️ 判据钉**值**（字面量，⛔ 不从 nodeCodesFor 派生——自指断言在值被改坏时不会红）。
    // 代价：两页共用一个点 ⇒ 不能单独授权「只看经营报表」（spec §9 第 8 条）。
    expect(ROUTE_NODE_CODES['agents-day-report']).toBe('finance.support_statistics')
    expect(ROUTE_NODE_CODES['agents-salesman-stats']).toBe('finance.support_statistics')
    // 上面「承重」那条已经自动把新路由纳入「表里不多不少」的比对；这条只钉**取值**。
    expect(realRouter.hasRoute('agents-day-report')).toBe(true)
  })

  it('⭐ D-0219：打手绩效列表（agents-performance）= finance.payroll ∪ order.list（任一即进）', () => {
    // 判据钉**值**（字面量，⛔ 不从 nodeCodesFor 派生——自指断言在值被改坏时不会红）。
    // 「任一即进」的语义由 `session.canNode` / `useMenu.canSeeItem` 的 `some` 实现，
    // 各自的测试钉住那一半；这里钉的是「这一条路由声明了哪两个节点」。
    expect(ROUTE_NODE_CODES['agents-performance']).toEqual(['finance.payroll', 'order.list'])
  })

  it('2026-10-23：APP与小程序数据概览（app-stats）复用 finance.support_statistics（⛔ 不新增节点）', () => {
    // 计划 `docs/plans/2026-10-23-app-mp-stats.md` §8 第一条：权限**复用**经营报表那一个点。
    // 判据钉**值**（字面量，⛔ 不从 nodeCodesFor 派生——自指断言在值被改坏时不会红）。
    expect(ROUTE_NODE_CODES['app-stats']).toBe('finance.support_statistics')
    // 上面「承重」那条已自动把新路由纳入「表里不多不少」的比对；这条只钉**取值**。
    expect(realRouter.hasRoute('app-stats')).toBe(true)
  })

  it('2026-10-19：QQ机器人设置（qqbot-settings）用本仓第三条自有节点（能单独授权，⛔ 不是复用）', () => {
    // `system.qqbot_settings` 是**自有**节点（`benchmark_id = None`）。
    // 判据钉**值**（字面量，⛔ 不从 nodeCodesFor 派生——自指断言在值被改坏时不会红）。
    expect(ROUTE_NODE_CODES['qqbot-settings']).toBe('system.qqbot_settings')
    // 上面「承重」那条已经自动把新路由纳入「表里不多不少」的比对；这条只钉**取值**。
    expect(realRouter.hasRoute('qqbot-settings')).toBe(true)
  })
})
