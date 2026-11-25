// @vitest-environment node
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * **04-smoke「冒烟清单没缩水」那条 CANDIDATES 常数钉子的下沉对拍网**（E-0200 同款下沉）。为什么要有这条：`04-smoke-all-pages.spec.ts` 的 `expect(CANDIDATES.length).toBe(N + EXPLICIT_ROUTES.length)` 是 Playwright e2e、被 vitest 的 `exclude: ['e2e/**']` 挡在**常跑门禁之外** ⇒ 它红了也没人看见——那个 N 自 T7 起就差 1，后续又沿错误基线按 ±1 挪一格 ⇒ **这条 e2e 断言从写下起就没绿过**（评审用 vitest import 真实 fixture 才实测出来）。
 * 本文件把「**e2e 里钉的那个常数 == 真实 fixture 读数**」搬进 vitest 常跑面：下一轮再想挪这个数，挪错当场红（⛔ 不是再复制一份数字——这里只读 04-smoke 的原文抽常数，两处数字仍只有 **04-smoke 一处写死**）。
 * **扫描面（如实声明）**：只钉 CANDIDATES 常数这一条（含「生成半与显式半不相交」这个换算前提）；04-smoke 同一用例里的 `REPLICA_SKIP.size === 43`、`generated.length === 46` 两枚写死数字钉仍在 e2e-only 面、**不在**本文件判别面内。**用「运行期算出来的 import 说明符」而不是静态 `import`**：与 `explicit-routes.spec.ts`（E-0200 先例）同一实测理由——静态 import 会把 `e2e/fixtures/routes.ts` 拉进 vitest 的 TS 程序报一批 TS 错；⛔ 别"简化"回静态 import。
 */
interface RoutesFixture {
  CANDIDATES: string[]
  EXPLICIT_ROUTES: string[]
  GENERATED_ROUTES: string[]
}

const fixtureSpecifier = '../../../e2e/fixtures/routes'

const { CANDIDATES, EXPLICIT_ROUTES, GENERATED_ROUTES } = (await import(
  fixtureSpecifier
)) as RoutesFixture

const here = dirname(fileURLToPath(import.meta.url))
const smokeSource = readFileSync(
  resolve(here, '../../../e2e/specs/04-smoke-all-pages.spec.ts'),
  'utf8',
)

/** 04-smoke 里那条钉子的形状（`expect(CANDIDATES.length, …).toBe(N + EXPLICIT_ROUTES.length)`）。 */
const PIN_RE = /\.toBe\((\d+) \+ EXPLICIT_ROUTES\.length\)/g

describe('04-smoke CANDIDATES 常数 ↔ 真实 fixture 读数（E-0200 同款下沉对拍）', () => {
  it('04-smoke 钉的常数与真实读数一致（e2e-only 的钉子被拉进常跑门禁）', () => {
    const matches = [...smokeSource.matchAll(PIN_RE)]
    const first = matches[0]
    expect(
      first,
      '04-smoke 里没抽到 `.toBe(N + EXPLICIT_ROUTES.length)`（钉子形状变了？）——响亮失败，不是静默放行',
    ).toBeDefined()
    expect(
      matches.length,
      '04-smoke 里该形状的钉子不止一处 ⇒ 本文件的抽取对象歧义了，请同一次收窄 PIN_RE',
    ).toBe(1)
    const pinned = Number(first?.[1])
    const real = CANDIDATES.length - EXPLICIT_ROUTES.length
    expect(
      pinned,
      `04-smoke 钉的常数 ${pinned} ≠ 真实读数 ${real}（CANDIDATES ${CANDIDATES.length} − EXPLICIT_ROUTES ${EXPLICIT_ROUTES.length}）。改路由要按真实 fixture 实测改准常数，⛔ 不按 ±1 推算（off-by-one 自 T7 起的教训）`,
    ).toBe(real)
  })

  it('换算前提：生成半与显式半不相交，且 CANDIDATES = 生成 + 显式', () => {
    const overlap = GENERATED_ROUTES.filter((p) => EXPLICIT_ROUTES.includes(p))
    expect(
      overlap,
      '生成半与显式半出现重叠 ⇒ CANDIDATES 去重后不再等于两半之和，上面那条「常数 = candidates − explicit」的换算前提破了',
    ).toEqual([])
    expect(CANDIDATES.length).toBe(GENERATED_ROUTES.length + EXPLICIT_ROUTES.length)
  })

  it('阳性对照：三个数都非空（防抽取坏掉后空对空恒真）', () => {
    // 抽取正则坏了会静默给空数组 ⇒ 前两条变成 0=0 恒真（AGENTS.md §5-G：网自己的断言也要能失败）。
    expect(CANDIDATES.length, 'CANDIDATES 空了（fixture 坏了？）').toBeGreaterThan(0)
    expect(GENERATED_ROUTES.length, 'GENERATED_ROUTES 空了（REPLICA_SKIP 把全部路由排除了？）').toBeGreaterThan(0)
    expect(EXPLICIT_ROUTES.length, 'EXPLICIT_ROUTES 空了（手抄清单被清空？）').toBeGreaterThan(0)
  })
})
