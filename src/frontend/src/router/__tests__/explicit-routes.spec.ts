// @vitest-environment node
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * **E-0200 的下沉对拍网**：「显式路由清单 ↔ `router/index.ts` 静态 island 路由**集合相等**」那条跨来源判据原本只住在 e2e（Playwright）里，本机没有 playwright 浏览器 ⇒ **存在但没人跑得动**（E-0200 原话：「跑不了的网 = 假保证」——历史上两次往 `router/index.ts` 加路由都没抄进 `EXPLICIT_ROUTES`，那条网红了也没人看见）。
 * 按 E-0200 登记的**下沉做法**：`e2e/fixtures/routes.ts` 只在**模块加载期** `readFileSync` 读文件、不需要浏览器 ⇒ 在 vitest 的 node 环境直接 import 它、把判据搬进**常跑**的面；import 成功本身就是该模块那几条**自检**（两份显式清单一致性 / 参数化登记表 ↔ router / 订单号形状 ↔ orders.ts）全过的判据。
 * ⚠️ **用「运行期算出来的 import 说明符」而不是静态 `import`**：静态 import 会把该文件拉进 tsconfig.vitest.json 的 TS 程序（开 `noUncheckedIndexedAccess`、没开 `allowJs` ⇒ 一批 TS7016 / TS2532；它真正的类型归宿是 tsconfig.node.json，那边今天就是绿的）。说明符走变量 ⇒ TS 不做静态解析，运行期仍由 vitest 加载**真模块**——判据一字不假。⛔ 别"简化"回静态 import：typecheck 门禁会当场红（实测读数见汇报）。
 */
interface RoutesFixture {
  EXPLICIT_ROUTES: string[]
  EXPLICIT_ROUTES_BY_NAME: Readonly<Record<string, string>>
  STATIC_ISLAND_ROUTES: string[]
  readStaticIslandRoutes: (source?: string) => string[]
}

const fixtureSpecifier = '../../../e2e/fixtures/routes'

const { EXPLICIT_ROUTES, EXPLICIT_ROUTES_BY_NAME, STATIC_ISLAND_ROUTES, readStaticIslandRoutes } = (await import(
  fixtureSpecifier
)) as RoutesFixture

describe('E-0200 下沉对拍：EXPLICIT_ROUTES ↔ router 静态 island 路由（恰好相等）', () => {
  it('显式清单与 router/index.ts 抽出的静态路由集合相等（漏抄 / 多抄都红）', () => {
    expect(
      [...EXPLICIT_ROUTES].sort(),
      // ⚠️ 消息必须是**单个**字面量 / 模板串（`+` 拼接会被 eslint 的 vitest/valid-expect 拒）。
      `显式路由清单与 router/index.ts 里抽出的静态 island 路由必须恰好相等（E-0200 同款漂移：改路由要同一次改 e2e/fixtures/routes.ts）`,
    ).toEqual(STATIC_ISLAND_ROUTES)
  })

  it('阳性对照：两侧都非空（防「空集恒真」）', () => {
    // 抽取正则坏了会静默给空数组 ⇒ 上面的 toEqual 变成「空对空恒真」。
    // 这条把「枚举面本身坏了」单独钉出来（AGENTS.md §5-G：结构性检查自己的断言也必须能失败）。
    expect(EXPLICIT_ROUTES.length, 'EXPLICIT_ROUTES 空了（手抄清单被清空？）').toBeGreaterThan(0)
    expect(
      STATIC_ISLAND_ROUTES.length,
      'STATIC_ISLAND_ROUTES 抽取为空（router 形状变了？）',
    ).toBeGreaterThan(0)
    // `EXPLICIT_ROUTES_BY_NAME` 与 `EXPLICIT_ROUTES` 的条数也在同一处对上
    // （两份清单漂移时该模块的加载期自检先抛、import 本身就失败；这条是同一事实的可读读数）。
    expect(Object.keys(EXPLICIT_ROUTES_BY_NAME).length).toBe(EXPLICIT_ROUTES.length)
  })
  it('独立测试页不进入 island 清单，但漏掉或增加其他静态页面仍会被发现', () => {
    const source = readFileSync(fileURLToPath(new URL('../index.ts', import.meta.url)), 'utf8')
    expect(STATIC_ISLAND_ROUTES).not.toContain('/wecom-client-lab')
    expect(STATIC_ISLAND_ROUTES).toContain('/orders')
    const removed = source.replace("path: '/orders',", "path: '/orders-removed',")
    expect(removed).not.toBe(source)
    expect(readStaticIslandRoutes(removed)).not.toEqual([...EXPLICIT_ROUTES].sort())
    const added = source + "\npath: '/extra-island',\n"
    expect(readStaticIslandRoutes(added)).not.toEqual([...EXPLICIT_ROUTES].sort())
  })

})
