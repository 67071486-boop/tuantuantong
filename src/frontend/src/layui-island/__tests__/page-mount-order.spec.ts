/**
 * 「**先挂 layui 样式，再塞页面标记**」这条挂载顺序的**全站兜底网**。
 *
 * ## 为什么这条顺序值得一张网
 *
 * island 页的样式表 `layui.css` **不是写死在 HTML 里**的：`layui-island/page.html` 的 `<head>` 是空的，
 * 样式由 `loadLayui()`（`@/layui/loader`）在**页面脚本跑起来之后**动态插一个 `<link>`。
 * 所以一旦某个页面把证据件 HTML 先塞进 `root`，浏览器就会**先按「什么都没穿」的样子画一遍**
 * （默认字体、没有边框的输入框/按钮/表格），等 131 KB 的 `layui.css` 到了才变正常 ——
 * 这就是业务方报的「切页面时框架从简陋闪一下变正常」（FOUC）。
 *
 * 实测（2026-09-22 业务方报障）：`order-myorderlist`（指派给我的订单）就是这么写的，
 * 而同批的 `order-insiderecorderlist`（内部抢单大厅）是先挂样式再塞标记 —— 所以「有的页面闪、有的不闪」。
 * 三个页面同形（另两个是 `emp-myemp` / `emp-billsummary`），都是手写渲染器时各自写反的。
 *
 * ## 判据（按文件，不按运行期）
 *
 * 60 个页面各自 mock 一遍依赖太贵，而这条顺序是**纯静态事实**，所以按源码判：
 *  1. 有 `root.innerHTML = …` 的文件 ⇒ 必须**也在同一个文件里** `await bootstrapIsland(`，
 *     且**调用行号更小**（先挂样式）。
 *  2. 有 `root.insertAdjacentHTML(` 的文件 ⇒ 它前面必须先有「挂样式」的落点：要么
 *     `await bootstrapIsland(`，要么共享渲染器的 `.mount(root…`（`artifactPage` / `replicaPage`
 *     内部第一句就是 `bootstrapIsland`）。**两条判据缺一不可**：`insertAdjacentHTML` 挂在
 *     `artifactPage({…}).mount(root, ctx)` 之后是合法的（共享渲染器已先挂样式；页面随后
 *     补一段自己的 `<style>` 不违反顺序），
 *     所以不能要求它后面必须再写一次 `bootstrapIsland`。
 *
 * ## 判据的边界（如实登记）
 *
 *  - 逐行**跳过注释行**：本仓注释里大量引用 `root.innerHTML` / `bootstrapIsland` 当判据句
 *    （文件头把它写成绝对句是老毛病），不跳过就会被注释喂出**假通过/假失败**。
 *    只跳「行首是 `*` / `//` / `/*`」的行；行尾注释不影响。
 *  - 它**看不出**「样式 `<link>` 还没到就塞了标记」这种**运行期**竞态（`injectCss()` 不 await
 *    load 事件）——本网只保证「塞标记时样式已经**开始**加载」，不保证已经到位。
 *    这正是为什么它不能替代 `order-myorderlist.spec.ts` 里那条**运行期**顺序钉子。
 *  - 判据靠 `readdirSync` 扫目录 ⇒ **新增页面自动进网**，不需要改本文件。
 */
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const PAGES_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../pages')

type Scan = {
  file: string
  /** `await bootstrapIsland(` 的调用行号。 */
  bootstrap: number[]
  /** `root.innerHTML = …` 的赋值行号。 */
  innerHtml: number[]
  /** `root.insertAdjacentHTML(` 的行号。 */
  insertAdjacent: number[]
  /** 共享渲染器的挂载落点 `.mount(root…` 的行号（`artifactPage` / `replicaPage`）。 */
  sharedMount: number[]
}

/** 注释行不算代码：本仓的注释自己就反复写这些符号（见文件头「判据的边界」）。 */
function isCommentLine(trimmed: string): boolean {
  return trimmed.startsWith('*') || trimmed.startsWith('//') || trimmed.startsWith('/*')
}

function scanFile(file: string): Scan {
  const out: Scan = { file, bootstrap: [], innerHtml: [], insertAdjacent: [], sharedMount: [] }
  const lines = readFileSync(path.join(PAGES_DIR, file), 'utf8').split('\n')
  lines.forEach((raw, index) => {
    const line = index + 1
    if (isCommentLine(raw.trim())) return
    if (raw.includes('await bootstrapIsland(')) out.bootstrap.push(line)
    if (/root\.innerHTML\s*=/.test(raw)) out.innerHtml.push(line)
    if (/root\.insertAdjacentHTML\s*\(/.test(raw)) out.insertAdjacent.push(line)
    if (/\.mount\(root/.test(raw)) out.sharedMount.push(line)
  })
  return out
}

const FILES = readdirSync(PAGES_DIR).filter((f) => f.endsWith('.ts'))
const SCANS = FILES.map(scanFile)

/** 只为了在失败信息里给出行号，定位比「期望 1 收到 3」快得多。 */
const at = (s: Scan, key: keyof Omit<Scan, 'file'>): string =>
  s[key].length === 0 ? '无' : `L${s[key].join(' / L')}`

describe('island 页挂载顺序：先挂 layui 样式，再塞页面标记', () => {
  it('扫到了全部页面文件（网不能是空的）', () => {
    // 判据的下界：页面目录里 `*.ts` 就是 island 页全集（`page.ts` 的注册表按文件名发现）。
    // 阈值取实测值再留余量——文件被误删/目录改路径时，下面两条断言会**平凡通过**，
    // 这一条是专门拦「网塌成空网」的（E-0065：结构性检查自己的断言也必须能失败）。
    expect(FILES.length).toBeGreaterThanOrEqual(60)
    expect(
      SCANS.filter((s) => s.innerHtml.length > 0).length,
      '塞标记的页面数量（本网真正在盯的对象）',
    ).toBeGreaterThanOrEqual(10)
  })

  it('没有任何页面在 bootstrapIsland 之前塞页面标记（否则会闪一下没样式的页面）', () => {
    const offenders = SCANS.filter(
      (s) => s.innerHtml.length > 0 && (s.bootstrap.length === 0 || s.bootstrap[0]! > s.innerHtml[0]!),
    ).map(
      (s) =>
        `${s.file}：塞标记 ${at(s, 'innerHtml')}，bootstrapIsland ${at(s, 'bootstrap')}` +
        `（${s.bootstrap.length === 0 ? '该文件里根本没有挂样式的调用' : '顺序反了'}）`,
    )
    expect(
      offenders,
      '这些页面会先画出没穿样式的骨架、再闪成正常（FOUC）。修法：把 await bootstrapIsland(…) 提到塞标记之前',
    ).toEqual([])
  })

  it('insertAdjacentHTML 也排在「挂样式」之后（bootstrapIsland 或共享渲染器的 mount）', () => {
    const offenders = SCANS.filter((s) => s.insertAdjacent.length > 0)
      .filter((s) => {
        const stylePoints = [...s.bootstrap, ...s.sharedMount]
        return stylePoints.length === 0 || Math.min(...stylePoints) > s.insertAdjacent[0]!
      })
      .map(
        (s) =>
          `${s.file}：insertAdjacentHTML ${at(s, 'insertAdjacent')}，` +
          `挂样式落点 bootstrapIsland ${at(s, 'bootstrap')} / 共享 mount ${at(s, 'sharedMount')}`,
      )
    expect(offenders, '这些页面在样式就位前就往 root 里补了标记').toEqual([])
  })
})
