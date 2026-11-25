// @vitest-environment jsdom
// 上面这行**必需**：本文件 `import realRouter from '@/router'`，而 `router/index.ts` 用的是
// `createWebHistory(import.meta.env.BASE_URL)`（内部会解构 `window`）——node 环境下 import 即抛。
import { describe, expect, it } from 'vitest'
import { BOTTOM_ITEMS, MENU_GROUPS, TOP_LEVEL_ITEMS } from '@/config/menu'
import { isClickable } from '@/composables/useMenu'
import realRouter from '@/router'

/**
 * **本文件里「菜单条目」的统一口径**：冻结 IA 的条目分三处声明（`TOP_LEVEL_ITEMS` / `MENU_GROUPS[].items` / `BOTTOM_ITEMS`），而 (b)/(c)/(d) 三条判据问的是**同一件事**（「每个菜单条目的声明与它的路由对不对得上」）⇒ 三处**必须一起进遍历**。
 * ⚠️ **这是加强审计、不是放宽**：把 `BOTTOM_ITEMS` 摘出去**不会**让任何一条断言变松——反过来，摘出去之后新加的底部条目**一条判据都不过**（不受 (b) 的节点码形态约束、也不受 (c) 的双源比对与 (d) 的非空声明约束），且 (c) 的 `compared.length` **也不会**跟着变 ⇒ 数字照样"对得上"。「把新清单纳入遍历」与「把字面量 +1」是**同一件事的两半**，缺一半就是「数字对、覆盖面其实没变」的假绿。
 */
const MENU_ITEMS = [...TOP_LEVEL_ITEMS, ...MENU_GROUPS.flatMap((g) => g.items), ...BOTTOM_ITEMS]

// 新版订单详情支持管理详情、本人订单和群成员三种访问范围，由各自 API 校验。
const WHITELIST = new Set(['login', 'home', 'not-found', 'v2', 'v2-home', 'v2-approvals', 'v2-internal-chat', 'v2-internal-chat-visitors', 'v2-internal-chat-window', 'visitor-chat', 'v2-order-detail-preview', 'v2-wecom-archive'])

/**
 * 判据形态：**RBAC 节点 code**。栏目级（`level=0`）单段（如 `order` / `my_orders` / `finance`）；功能点（`level=1`）`<module>.<action>`（如 `order.list` / `system.role_authority`）。
 * ⚠️ 不能再用 `"app_label.codename"` 的形态：栏目码**没有点号**，旧正则会把 `ROUTE_NODE_CODES` 里 18 条栏目码当场判红（短信整块下线后 68 → 64，专栏目码条数未变）。
 */
const NODE_CODE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$/

/**
 * 「这条路由声明了准入判据」= 非空 `meta.permissions` **或** `meta.requiresAccountAdmin === true`。第二条轴（【系统人员】那批路由）的由来：它们的后端准入是 `IsAccountAdmin` 这个**权限类**、不是一个节点 code——硬塞进 `permissions` 会让 `session.canNode()` 的「节点 code 交集」语义被污染、`useMenu` 的条目级 `canSeeItem` 也会跟着错 ⇒ 新开一条**独立**判据轴，审计把两者都算「已声明」。摩擦仍在：新路由两条都不声明就会露头。
 */
function hasDeclaration(r: { meta: { permissions?: string[]; requiresAccountAdmin?: boolean } }): boolean {
  return (r.meta.permissions ?? []).length > 0 || r.meta.requiresAccountAdmin === true
}

describe('路由与菜单的权限声明审计（spec D8 / §6.1 #26）', () => {
  it('(a) 除白名单外每条路由都声明了准入判据（permissions 或 requiresAccountAdmin）', () => {
    const offenders = realRouter
      .getRoutes()
      .filter((r) => !WHITELIST.has(String(r.name ?? '')))
      .filter((r) => !hasDeclaration(r))
      .map((r) => String(r.name))
    expect(offenders).toEqual([])
  })

  it('(a) 无声明路由的集合恰为白名单 —— 新路由若**忘了**声明权限就会在这里露头', () => {
    // 这一条才是承重的：上一条对"今天的路由全在白名单里"是平凡真。
    // 判据是「**没有**声明 permissions 的路由 name 集合」恰好等于白名单：
    // 新增一条无声明的路由 → 它的 name 出现在左边 → 不等 → 红。
    const names = realRouter
      .getRoutes()
      .filter((r) => !hasDeclaration(r))
      .map((r) => String(r.name))
      .sort()
    expect(names).toEqual([...WHITELIST].sort())
  })

  it('(b) 所有声明的节点 code 形态正确（路由 + 菜单条目）', () => {
    for (const r of realRouter.getRoutes()) {
      for (const c of r.meta.permissions ?? []) expect(c).toMatch(NODE_CODE)
    }
    for (const i of MENU_ITEMS) {
      for (const c of i.permissions ?? []) expect(c).toMatch(NODE_CODE)
    }
  })

  it('(c) 菜单条目与其路由的 meta 声明一致（本次接线后这条**承重**）', () => {
    // 收缩白名单到 3 条之前，比对面只剩【系统人员】那几条、且两边都是 `[]` ⇒ 恒等，对真实分歧**不承重**。本次接线后比对面 = 菜单里所有能查到真路由的条目（除 `home`），比对的是**两个独立来源**：菜单条目的字面量 vs 路由 meta（后者由 `config/routePermissions.ts` 经 `nodeCodesFor()` 供值）。
    // 两份都从同一张表派生会让这条变成同义反复（E-0024 族）——菜单侧刻意**手写**，就是为了让这里仍然承重。
    const byName = new Map(realRouter.getRoutes().map((r) => [String(r.name ?? ''), r]))
    const menuItems = MENU_ITEMS.filter((i) => i.routeName !== undefined)
    for (const i of menuItems) {
      const route = byName.get(i.routeName!)
      // 路由表里没有该 name = 菜单里表达为"即将开放"（置灰），**不是**错误；
      // 白名单里的路由不要求菜单声明（今天只有 `home`）。
      if (!route || WHITELIST.has(String(route.name))) continue
      expect([...(i.permissions ?? [])].sort()).toEqual([...(route.meta.permissions ?? [])].sort())
    }
  })

  it('(c) 所有 `routeName` 有真路由的菜单条目都落进比对（`home` 在白名单，跳过）', () => {
    // 条数是承重的：漏进 MENU_ITEMS（尤其 BOTTOM_ITEMS）或下架菜单却不改这个数就会红。
    // 下面每条 toContain 证明这个数是这些条目凑出来的，不是别处漂移碰巧相等。
    const byName = new Map(realRouter.getRoutes().map((r) => [String(r.name ?? ''), r]))
    const compared = MENU_ITEMS.filter((i) => {
      const route = i.routeName === undefined ? undefined : byName.get(i.routeName)
      return route !== undefined && !WHITELIST.has(String(route.name))
    })
    // 75 → 73：2026-11-10 第二批客户维护删两条目（REPLICA_SKIP 留痕，路由不再生成）。
    expect(compared.length).toBe(73)
    expect(compared.map((i) => i.routeName)).toContain('system-role-authority')
    expect(compared.map((i) => i.label)).toContain('角色职位权限')
    // 阳性对照（AGENTS.md §5-G：结构网的断言也要能失败）：本次新增的那条确实落在比对面里，否则上面 61 → 62 这个数字可能只是别处漂移凑出来的。
    // ⚠️ 这个数字是 **61**（鸡蛋铺系统同步下架后）⇒ 这句阳性对照仍指向 `open-apps`（它照样在比对面里），「61 是别处漂移凑出来的」照样被它挡住。
    expect(compared.map((i) => i.routeName)).toContain('open-apps')
    // ⭐ 侧栏最底部那条：它**只可能在** `BOTTOM_ITEMS` 里 ⇒ 这条对照是
    // 「纳入遍历」这件事本身的阳性对照（`open-apps` 那条覆盖不到它）。
    expect(compared.map((i) => i.routeName)).toContain('feedback')
    // ⭐ 企微群码那条的阳性对照：总数里那一条确实是它，
    // ⛔ 不是别处漂移凑出来的（`AGENTS.md` §5-G：结构网的断言也要能失败）。
    expect(compared.map((i) => i.routeName)).toContain('wecom-group-qr')
    // ⭐ 【开放平台 → 打手对接】那条的阳性对照。
    // 它在 `MENU_GROUPS`（第三个自有功能页）⇒ 上面两条对照都覆盖不到它。
    expect(compared.map((i) => i.routeName)).toContain('taker-apps')
    expect(compared.map((i) => i.label)).toContain('打手对接')
    // ⭐ 【系统人员 → 自助注册】的阳性对照——
    // 总数里那一条确实是它，⛔ 不是别处漂移凑出来的（同上）。
    expect(compared.map((i) => i.routeName)).toContain('self-registration')
    expect(compared.map((i) => i.label)).toContain('自助注册')
    // ⭐ 【财务相关 → 经营报表】的阳性对照——
    // 总数里那一条确实是它，⛔ 不是别处漂移凑出来的（同上）。
    expect(compared.map((i) => i.routeName)).toContain('agents-day-report')
    expect(compared.map((i) => i.label)).toContain('经营报表')
    // ⭐ 【第三方平台 → QQ机器人设置】的阳性对照——
    // 总数里那一条确实是它，⛔ 不是别处漂移凑出来的（同上）。
    expect(compared.map((i) => i.routeName)).toContain('qqbot-settings')
    expect(compared.map((i) => i.label)).toContain('QQ机器人设置')
    // ⭐ 原阳性对照 `agents-withdraw-review` /「打手提现审核」**已并入财务合并页**（路由还在、菜单条目没了 ⇒ 不在比对面里）⇒ 对照换成两条：「打手钱包」（合并页）与「闲管家收款明细」（新页）。
    // ⚠️ 本次总数**不变**靠的是「删两条 + 增两条」的 −2/+2 对冲，所以这两条对照守的**不是**「数字变大」，而是「这两条真的在比对面里」：漏登记任何一条（routePermissions 漏项 / 路由没建 / 菜单没声明）它们就会从这里消失。
    expect(compared.map((i) => i.routeName)).toContain('agents-taker-wallet')
    expect(compared.map((i) => i.label)).toContain('打手钱包')
    expect(compared.map((i) => i.routeName)).toContain('xgj-receipts')
    expect(compared.map((i) => i.label)).toContain('闲管家收款明细')
    // ⭐ [APP与小程序 → 数据概览] 与 [发布文章作品] 两条的阳性对照，
    // ⛔ 不是别处漂移凑出来的。
    expect(compared.map((i) => i.routeName)).toContain('app-stats')
    expect(compared.map((i) => i.label)).toContain('数据概览')
    expect(compared.map((i) => i.routeName)).toContain('app-mp-articles')
    expect(compared.map((i) => i.label)).toContain('发布文章作品')
  })

  it('(d) 除 `home` 外，每个能查到路由的菜单条目都声明了非空 permissions', () => {
    const byName = new Map(realRouter.getRoutes().map((r) => [String(r.name ?? ''), r]))
    const offenders = MENU_ITEMS
      .filter((i) => {
        const route = i.routeName === undefined ? undefined : byName.get(i.routeName)
        return route !== undefined && !WHITELIST.has(String(route.name))
      })
      .filter((i) => (i.permissions ?? []).length === 0)
      .map((i) => i.label)
    expect(offenders).toEqual([])
  })
})

describe('【游戏维护】的接线状态（spec §6.1 T1）', () => {
  it('两条全部可点——判据走 `isClickable` + 真路由表，不是读 `soon`', () => {
    // 业务裁剪（D-0036）删掉那两条，业务方又要求把「订单项目分类管理」与「常用代练项目管理」合并成一页 ⇒ 清单**为 2 条**（游戏管理 + 合并后的「订单项目分类与常用项目」）。
    // 这不是放宽：判据仍是**真**路由表上的 `isClickable`。
    const group = MENU_GROUPS.find((g) => g.name === '游戏维护')
    expect(group, '菜单里没有【游戏维护】分组').toBeDefined()
    const hasRoute = (name: string) => realRouter.hasRoute(name)

    const clickable = group!.items
      .filter((i) => isClickable(i, hasRoute))
      .map((i) => i.label)

    // 逐字对齐组内冻结顺序：两条全部接线。
    expect(clickable).toEqual([
      '游戏管理',
      '订单项目分类与常用项目',
    ])
  })
})

it('消息推送订阅直达地址须经工作室管理员权限校验', () => {
  const resolved = realRouter.resolve('/v2/wxpusher-settings')
  expect(resolved.name).toBe('v2-wxpusher-settings')
  expect(resolved.meta.requiresAccountAdmin).toBe(true)
})

it('内部沟通配置直达地址须经工作室管理员权限校验', () => {
  const resolved = realRouter.resolve('/v2/internal-chat-settings')
  expect(resolved.name).toBe('v2-internal-chat-settings')
  expect(resolved.meta.requiresAccountAdmin).toBe(true)
})

it('仅访客注册公开，访客接待与内部沟通宽窗维持登录校验', () => {
  expect(realRouter.resolve('/visitor-chat').meta.requiresAuth).toBe(false)
  expect(realRouter.resolve('/v2/internal-chat-visitors').meta.requiresAuth).not.toBe(false)
  expect(realRouter.resolve('/v2/internal-chat-window').meta.requiresAuth).not.toBe(false)
})
