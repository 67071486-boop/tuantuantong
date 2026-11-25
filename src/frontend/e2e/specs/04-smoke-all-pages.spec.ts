import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { expect, test, type BrowserContext, type Locator, type Page } from '@playwright/test'

import { FRONTEND_URL, SHOTS_DIR } from '../config.mjs'
import { login } from '../fixtures/auth'
import { expectNoPageErrors, observe, type Observations } from '../fixtures/observers'
// ⚠️ 候选清单的**唯一出处**在 `fixtures/routes.ts`（本文件原先自带一份抽取实现，
// 2026-09-24 T2 把它整段上移，与 `05-role-action-audit.spec.ts` 共用同一份——
// 两份实现会漂移，而漂移的那一份只会**静默少测**，见本仓 E-0021）。
// ⛔ 本文件下面那三条结构兜底断言（REPLICA_SKIP 43 / 生成 46 / 集合相等）**一条都没放宽**。
// （41→43 / 48→46：2026-11-10 第二批客户维护删两页，2 个对标 key 登记 REPLICA_SKIP。）
import { CANDIDATES, EXPLICIT_ROUTES, GENERATED_ROUTES, REPLICA_SKIP, STATIC_ISLAND_ROUTES } from '../fixtures/routes'
import { ISLAND_IFRAME, ISLAND_MOUNT, ISLAND_UNREGISTERED } from '../fixtures/selectors'

const generated = GENERATED_ROUTES

/**
 * ③ 的判据本体（**唯一出处**，页面用例与下面那条「哨兵对照」用例都调它）：
 * 读 `#app` 的**原文**，判里面有没有未登记哨兵。
 *
 * ⛔ **参数是 `Locator`、文本在函数里自己读**（终审 MUST-FIX）——这不是风格问题：
 * 原签名收的是一个**字符串**，于是页面用例那一侧**可以**单独传一个「已删过空白的串」
 * （常驻对照仍传 `root.textContent()` 的原文）⇒ **两边执行的是不同形状的调用**，
 * 对照守的是 **helper**、不是**页面用例那一行**：页面用例从此静默恒真，而对照照样绿。
 * 改成收 `Locator` 之后，**比对永远发生在同一个地方**：谁把归一化塞进 `isUnregistered()`，
 * 页面用例与常驻对照会**同时**用上它 ⇒ 常驻对照当场红（见本轮报告里的变异证据）。
 *
 * ⛔ 有意**不删空白**。订正（T7 修复轮，评审重要项 1）：哨兵 `ISLAND_UNREGISTERED`
 * （`'未登记的 island 页面'`）**含 ASCII 空格**，而任务书原文先把文本 `replace(/\s+/g, '')`
 * 再 `toContain(哨兵)` ⇒ **永远不命中**（判据恒真、不可能红，且从未做过反向对照）。
 * 两侧要么同一套归一化、要么都用原文——这里统一用**原文**（`page.ts:50` 渲染的就是原文）。
 */
async function isUnregistered(root: Locator): Promise<boolean> {
  return ((await root.textContent()) ?? '').includes(ISLAND_UNREGISTERED)
}

/**
 * 体检表一行。`status` **必须是可变字面量联合**（不是 `as const`）——否则 `row.status = 'passed'`
 * 会被推断成赋值给 `'failed'` 而报 TS2322（本行注释就是踩过之后写的）。
 * ⛔ 没有 `status` 的话，`.tools/e2e/smoke-report.json` 里一行「路由挂了」与一行
 * 「页面本来就什么都没渲染」长得**一模一样**（都是几个 0）——那正是本网最该留痕的东西被抹平。
 */
type SmokeRow = {
  path: string
  status: 'passed' | 'failed'
  /** 失败时的错误首行（仅 `status === 'failed'` 时有意义；截断到 300 字，避免把 HTML 快照塞进去）。 */
  error: string
  renderedChars: number
  consoleErrors: number
  clientErrors: number
  serverErrors: number
}

/**
 * 体检表的**每行一个文件**的落点（`<repo>/.tools/e2e/smoke-rows/<slug>.json`）。
 *
 * ⛔ **为什么不是模块级数组**：Playwright 在**某条用例失败后会重启
 * worker 进程**，模块被重新 import ⇒ 模块级数组清零。实测：60 条路由里第 38 条挂了之后，
 * 体检表**只剩重启后的 23 行**，而「第一个失败的那一行」——最该留痕的那条——**恰好不在表里**。
 * 改成「每条用例自己写一个小文件」后，重启也丢不了：行只增不减，聚合发生在 `afterAll`。
 */
const ROWS_DIR = resolve(SHOTS_DIR, '..', 'smoke-rows')

/** 路由 → 行文件名（路径里的非单词字符一律换成 `_`，与截图命名同一套规则）。 */
const rowFileOf = (path: string): string => resolve(ROWS_DIR, `row${path.replace(/[^\w]+/g, '_')}.json`)

let context: BrowserContext
let page: Page

test.beforeAll(async ({ browser }) => {
  // 重启后这里会再跑一次；`recursive: true` 对已存在的目录是幂等的 ⇒ 不会清掉已写的行。
  mkdirSync(ROWS_DIR, { recursive: true })
  context = await browser.newContext({ baseURL: FRONTEND_URL })
  page = await context.newPage()
  // 冒烟用**系统管理员**：它的节点判据短路成全集 ⇒ 全站可开（`rbac/permissions.py:72-73`）。
  await login(page, 'admin')
})

test.afterAll(async () => {
  await context?.close()
  // 从**每个小文件**聚合（顺序按 `CANDIDATES`，与跑的顺序无关）⇒ worker 重启也丢不了行。
  // ⚠️ 目录由 `e2e/run.mjs` 在每次整体运行前清空一次（`beforeAll` **不能**清：重启会再跑它）。
  // 文件**保持数组形状**（不改成对象）：README 与设计文档 §13 描述的就是「每行一条路由」。
  const rows: SmokeRow[] = []
  const missing: string[] = []
  for (const path of CANDIDATES) {
    try {
      rows.push(JSON.parse(readFileSync(rowFileOf(path), 'utf8')) as SmokeRow)
    } catch {
      missing.push(path)
    }
  }
  writeFileSync(resolve(SHOTS_DIR, '..', 'smoke-report.json'), JSON.stringify(rows, null, 2))
  // ⛔ 「缺行」必须**看得见**：体检表少一行而没人说，就正落回「表比它看起来少」这个坑。
  // 这里不抛（真正的失败已经由用例自己报了），但用醒目的前缀打到运行输出里。
  if (missing.length) {
    console.warn(`⚠️ 体检表缺 ${missing.length} 行（这些路由没写出自己的行）：${missing.join('、')}`)
  }
  console.log(`📋 体检表：${rows.length} 行（期望 ${CANDIDATES.length}）→ .tools/e2e/smoke-report.json`)
})

test('结构兜底：冒烟清单没缩水（这条自己也要能变红）', () => {
  // 三个数都对不上时响亮失败，而不是静默少测几页。
  // 本文件是 Playwright，不在 vitest 常跑里；常跑对拍在
  // `src/router/__tests__/smoke-candidates-constant.spec.ts`。
  // `REPLICA_SKIP` 第 9 类的注释不要用单引号包词：`fixtures/routes.ts` 的正则
  // 会把该块里所有单引号串当成 key，注释里的引号会把条数撑大。
  expect(REPLICA_SKIP.size, 'router/index.ts 的 REPLICA_SKIP 条数变了').toBe(43)
  expect(generated.length, '生成的复刻路由条数变了').toBe(46)
  // ⚠️ 这一条是**自洽**的（右半边跟着 `EXPLICIT_ROUTES` 数组走）⇒ 本文件**不需要**为「显式清单 +1」
  // 改任何数字：2026-10-09（企微群码线 T6）新增 `/thirdparty/wecom-group-qr` 之后，
  // `EXPLICIT_ROUTES` 14 → 15、`CANDIDATES` 61 → 62，两边**同一次**跟着变，本条照旧绿。
  // ⇒ 「显式那一半没漏抄 / 没多抄」由下面那条**跨来源**判据（与 `router/index.ts` 抽出的
  // `STATIC_ISLAND_ROUTES` 集合相等）承担 —— 它同样跟着走，⛔ 不靠这里的数字。
  expect(CANDIDATES.length, '冒烟清单总条数变了').toBe(46 + EXPLICIT_ROUTES.length)
  // ⛔ 上面那条 `48 + EXPLICIT_ROUTES.length` **自洽地**跟着数组走：删掉一条显式路由，两边同时 −1
  // ⇒ 它**照样绿**（T7 评审重要项 2 实测指出）。所以显式那一半另有**跨来源**判据——
  // 与 `router/index.ts` 里抽出的静态 path 字面量**集合相等**（两份实现互不依赖）。
  expect(
    [...EXPLICIT_ROUTES].sort(),
    '显式路由清单与 router/index.ts 里抽出的静态 island 路由对不上（漏抄 / 多抄）',
  ).toEqual(STATIC_ISLAND_ROUTES)
})

for (const path of CANDIDATES) {
  test(`冒烟：${path}`, async () => {
    // 体检表**先建行**（评审 Minor 4）：任何断言失败也保证这一条路由在表里有行，
    // 否则失败的页面反而从体检表里消失、表长会缩——那正是最该留痕的页面。
    const row: SmokeRow = {
      path,
      status: 'failed',
      error: '',
      renderedChars: 0,
      consoleErrors: 0,
      clientErrors: 0,
      serverErrors: 0,
    }

    const obs: Observations = observe(page)

    const frame = page.frameLocator(ISLAND_IFRAME)
    const root = frame.locator(ISLAND_MOUNT)

    try {
      // ⛔ **导航本身也要在 try 里**（终审 Minor）：`page.goto(path)` 原先在 try 之外，
      // 于是「这条路由根本打不开」（DNS / 连接被拒 / 超时）会抛在 finally 之外 ⇒
      // 那一行留在表里、却永远不被补上观测，且**没有**任何失败标记。
      await page.goto(path)

      // ① iframe 得先出来（不存在的路由没有 island 宿主 ⇒ 这里红）。
      await expect(root, `${path}：island iframe 没挂载出来`).toBeAttached({ timeout: 20_000 })

      // ② 等**终态**（自等）：`#app` 里有元素子节点（页面模块挂上了），或只有文字（`page.ts` 的两个报错分支）。
      // ⛔ 任务书原文在 ① 之后**立刻**读 `body.innerText` 并断 `length > 0` —— 实测那是**竞态**：
      // `#app` 是 `layui-island/page.html:17` 的**静态**节点，iframe 文档一开就 attached，
      // 而页面模块（`page.ts` 的 `import.meta.glob` 动态 import）此时还没挂上 ⇒ 读到空串。
      // 本机实测 9 条列表页因此假红（`tAttached` 325–368ms vs 首字 369–586ms）。
      await expect
        .poll(
          () =>
            root.evaluate((el) =>
              el.childElementCount > 0
                ? 'mounted'
                : (el.textContent ?? '').trim()
                  ? 'text-only'
                  : '',
            ),
          {
            timeout: 20_000,
            // ② 自己的文案（评审 Minor 3）：① 是「iframe 都没出来」，② 是「iframe 在、但 `#app` 一直空」，
            // 两者不是一件事，不能共用一句话。
            message: `${path}：island 页面没渲染出来（iframe 在，但 #app 既没元素也没文字）`,
          },
        )
        .not.toBe('')

      // 读渲染文本（**只进体检表，不当判红判据**）。
      // 给「先挂元素、后填文字」的页面最多 1s：实测整套一起跑（机器有负载）时
      // `/system/role-authority` 会在表格文字填进来之前被读到 0 字符——而同一张截图里它完整渲染了。
      // `/customers/analysis` 结构上就没有文字 ⇒ 允许 0 收尾。
      let text = ''
      for (let i = 0; i < 10; i++) {
        text = (await frame.locator('body').innerText()).replace(/\s+/g, '')
        if (text.length > 0) break
        await page.waitForTimeout(100)
      }
      row.renderedChars = text.length

      // ③ 页面 key 没登记 ⇒ 判红（`page.ts:50` 的专属文案；先判它，红得最具体）。
      // ⛔ 传的是**锚点本身**（`Locator`），文本由 `isUnregistered()` 自己读——两侧同一个调用形状
      // （终审 MUST-FIX；那条判据的「会红」由下面的常驻对照片用例守）。
      expect(await isUnregistered(root), `${path}：页面 key 没登记`).toBe(false)

      // ④ 页面模块得**真的挂上**：`#app` 里要有元素子节点。
      // ⚠️ 不能只断「文字长度 > 0」（任务书原判据）：`/customers/analysis` 零文字但结构完整
      // （4 个空 ECharts 容器）⇒ 只看文字会把它**假红**。
      // ⚠️ 也不能「文字非空」就放行：`page.ts:55` 的 `.catch`（页面模块 import / 挂载抛错）只往
      // `#app` 写一句错误文案、**不建元素**，原判据会把它**当绿**——而「页面崩了」正是本网要抓的。
      // §4.5 有人为构造的对照证明这条判据真的会因此变红；本轮 `--grep 冒烟` 的 60 条真实路由
      // 全绿，即没有一条真实路由走这个纯文字分支（**只对本轮实测范围成立**，不是绝对句）。
      expect(
        await root.evaluate((el) => el.childElementCount),
        `${path}：iframe 内容为空（island 没挂载出来或页面白屏）`,
      ).toBeGreaterThan(0)

      expectNoPageErrors(obs, path)

      await page.screenshot({
        path: resolve(SHOTS_DIR, `smoke${path.replace(/[^\w]+/g, '_')}.png`),
        fullPage: true,
      })
      row.status = 'passed'
    } catch (err) {
      // 失败也要能在表里区分「这条路由挂了」与「这条路由合法地什么都没渲染」。
      row.error = String(err).split('\n')[0].slice(0, 300)
      throw err
    } finally {
      // 即使中途红，也把**当时**的观测补进那一行（数量可能还没稳定，但胜于没有）。
      row.consoleErrors = obs.consoleErrors.length
      // ⚠️ 4xx 也进体检表（只记录、不判红）：它正是「某页在打自己没权限的接口」的信号来源。
      row.clientErrors = obs.clientErrors.length
      row.serverErrors = obs.serverErrors.length
      // ⛔ 在 `finally` 里**落盘**（不是 push 进模块级数组）：worker 因失败重启时，模块级数组会清零，
      // 而这条路由恰恰是「第一个失败」⇒ 它的行会从体检表里消失。
      mkdirSync(ROWS_DIR, { recursive: true })
      writeFileSync(rowFileOf(path), JSON.stringify(row, null, 2))
    }
  })
}

/**
 * **常驻**反向对照（评审重要项 1）：③ 的哨兵判据必须对**未登记 key** 为真。
 * 一次性变异证明不了「以后不被改坏」——这条每次跑都验同一个 `isUnregistered()`，
 * 谁再把它改回「删空白再比对含空格的哨兵」，这条会当场红。
 * ⚠️ 它守得住的前提是**两边调用形状相同**（终审 MUST-FIX）：`isUnregistered()` 现在收 `Locator`、
 * 自己在内部读文本，所以上面那条页面用例**不可能**单独做一遍归一化 ⇒ 谁改坏归一化，两边一起红。
 * 直接开 island 宿主页本身（不经 Vue 壳）：`?page=__no_such_page__` 没登记。
 */
test('对照：③ 的哨兵判据对未登记 key 必须为真（防它退化成恒真）', async () => {
  await page.goto(`${FRONTEND_URL}/layui-island/page.html?page=__no_such_page__`)
  const root = page.locator(ISLAND_MOUNT)
  await expect(root, 'island 宿主页没打开').toBeAttached({ timeout: 20_000 })
  await expect
    .poll(async () => isUnregistered(root), {
      timeout: 20_000,
      message: '③ 的哨兵判据没命中未登记页 —— 它被改坏了（例如一侧删了空白）',
    })
    .toBe(true)
  expect(
    await root.evaluate((el) => el.childElementCount),
    '未登记页只该写文字、不该建元素（④ 就是靠这一点把它与「挂上了」区分开）',
  ).toBe(0)
})
