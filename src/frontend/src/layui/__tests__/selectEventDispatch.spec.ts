/**
 * 「layui 的 `select(<filter>)` 只在**真人点 `dd`** 时派发」的**框架行为判据**。
 *
 * 为什么值得单独一条：`order-addorder.ts` 的「客服接管」状态位 `userPickedGame` 的置位点就在
 * `callForm('on','select(selectGame)', …)` 的回调里（`:1768-1771`，spec §3.2 冻结的那个接入点），
 * 而「按内容自动选游戏」的**承重前提**是：**程序化写 `select.value` + `layui.form.render('select')`
 * 不会**触发那个回调。在本文件之前，这条前提只有「读 `public/layui/layui.js` 源码」一条证据——
 * 派发点在一个委派处理器里（`g.on("click","dd", …)`，偏移 263498 的
 * `layui.event.call(this,$,"select("+n+")",{elem:y[0],value:a,…})`）。
 * ⇒ 将来升级 layui 若语义变了，症状是**静默**的：自动切游戏会把自己关掉，此后这一单再也不自动切。
 * 本文件把那条前提变成**可重跑**的判据。页面加载的就是这份 vendored 副本（`src/layui/loader.ts:63`）。
 *
 * 手法照抄仓内先例 `layui-island/pages/__tests__/order-addorder-formselects.spec.ts`：
 * `new JSDOM(…, { runScripts: 'dangerously' })` 把真 layui 以 `<script>` 塞进去，回调在
 * **JSDOM 的 realm 里**（`window.eval`）注册，加载路径与页面一致（`layui.use(['form'], …)`）。
 * ⚠️ 先例的文件头记了一条实测教训：跨 realm 注册的回调在同族插件里**根本注册不进去**。
 * 本文件沿用同一纪律，但**不依赖**「跨 realm 可不可行」——下面 ① 的正向控制就是它的判据：
 * 回调若没登记上，① 会红，而**不会**伪装成 ② 的「一次都没派发」（那才是恒真的假绿）。
 *
 * ⚠️ 三条断言缺一不可（见各 `it` 的注释）：
 *   ① **正向控制** —— 点一个渲染出来的 `dd` ⇒ 回调被调用**一次**、且拿到的 `value` 是被点的那个值；
 *   ② **反向判据** —— 程序化写值 + `render` ⇒ 回调**一次都没被调用**；
 *   ③ **伴随判据** —— 那次 `render` **确实生效了**（替身节点上当前项已换成新值）。
 * 少了 ③，② 只证明「`render` 什么都没做」，而不是「`render` 不发事件」——那是假绿。
 *
 * ⚠️ 夹具值一律**合成值**（E-0077）；标记与证据件 `public/replica/order-addorder.html:105` 同形
 * （`<select id="gameName" name="gameName" lay-filter="selectGame">`，没有 `lay-search`）。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { JSDOM } from 'jsdom'
import { describe, expect, it } from 'vitest'

const LAYUI_JS = path.resolve(process.cwd(), 'public/layui/layui.js')

/** 证据件里那个游戏下拉的**同形**标记（选项值是两个合成的游戏 id）。 */
const HTML = `
  <form class="layui-form" lay-filter="orderDialog">
    <select id="gameName" name="gameName" lay-filter="selectGame">
      <option value="2">绝区零</option>
      <option value="5">三角洲行动</option>
    </select>
  </form>`

type ProbeWindow = JSDOM['window'] & {
  eval: (src: string) => unknown
  /** 真派发过来的 `obj.value`（只由 JSDOM realm 里那个回调写入）。 */
  __calls: string[]
  __resolveReady: () => void
}

/**
 * 建一份全新文档：真 vendored layui + 一个 `select#gameName`，按**页面同一条**路径加载 form 模块，
 * 渲染出替身节点，并在 **JSDOM realm** 里注册 `select(selectGame)` 回调（见文件头）。
 *
 * `layui.use` 的模块回调是**异步**的（实测：脚本塞进去之后 `layui.form` 已存在，但 `use` 的回调
 * 要等下一轮才跑）⇒ 这里等它自己通知，不猜时长。
 */
async function setupSelect(): Promise<ProbeWindow> {
  const dom = new JSDOM(`<!doctype html><html><body>${HTML}</body></html>`, {
    runScripts: 'dangerously',
    url: 'http://localhost/',
    pretendToBeVisual: true,
  })
  const w = dom.window as unknown as ProbeWindow
  const script = w.document.createElement('script')
  script.textContent = readFileSync(LAYUI_JS, 'utf8')
  w.document.body.appendChild(script)

  const ready = new Promise<void>((resolve) => {
    w.__resolveReady = resolve
  })
  w.eval(`
    window.__calls = [];
    window.layui.use(['form'], function () {
      layui.form.render('select');
      layui.form.on('select(selectGame)', function (obj) { window.__calls.push(String(obj.value)); });
      window.__resolveReady();
    });
  `)
  await ready
  return w
}

/** 渲染出来的替身节点里**当前项**那个 `dd`（layui 把替身挂在 `<select>` 的相邻兄弟上）。 */
function currentDd(w: ProbeWindow): Element | null {
  return w.document.querySelector('#gameName + .layui-form-select dd.layui-this')
}

/**
 * 按一个键。
 * ⚠️ layui 读的是 `event.keyCode`（偏移 261173 的 `40===e` / `13===e`），而 jsdom **不会**从
 * `key: 'ArrowDown'` 推导 `keyCode`（实测它恒为 `0`）⇒ 必须显式给 `keyCode`/`which`。
 */
function pressKey(w: ProbeWindow, target: Element, keyCode: number): void {
  target.dispatchEvent(
    new w.KeyboardEvent('keydown', {
      bubbles: true,
      cancelable: true,
      keyCode,
      which: keyCode,
    } as KeyboardEventInit),
  )
}

describe('真 vendored layui：`form.on("select(<filter>)")` 只在真人点 `dd` 时派发', () => {
  it('① 点 dd ⇒ 回调一次且 value = 被点的值；② 程序化写值 + `render` ⇒ 一次都不派发；③ 但那次 render 确实生效', async () => {
    // ── ① 正向控制：证明这套 harness **真的能观察到事件** ──
    // 没有它，② 的「0 次」无法与「回调压根没登记上」区分（后者是恒真的假绿）。
    const a = await setupSelect()
    a.eval(`document.querySelector('#gameName + .layui-form-select dd[lay-value="5"]').click()`)
    expect(a.__calls).toEqual(['5'])

    // ── ② 反向判据（本用例的主角）：照 `order-addorder.ts` 自动切游戏那一步的写法，程序化写值 ──
    const b = await setupSelect()
    // 起点：`render` 之后当前项是第一条（下面 ③ 拿它当对照）
    expect(currentDd(b)?.getAttribute('lay-value')).toBe('2')
    b.eval(`
      document.querySelector('#gameName').value = '5';
      layui.form.render('select');
    `)
    expect(b.__calls).toEqual([])

    // ── ③ 伴随判据（⛔ 不能省）：那次 `render` **确实生效了** ──
    // 否则 ② 只说明「render 什么都没做」，而不是「render 不发事件」——那是假绿。
    expect(currentDd(b)?.getAttribute('lay-value')).toBe('5')
    expect(currentDd(b)?.textContent).toBe('三角洲行动')
    // 隐藏 `<select>` 那条腿也一起到新值（页面读的就是它）
    expect((b.document.querySelector('#gameName') as HTMLSelectElement).value).toBe('5')
  })

  it('④ 下拉搜索框里 ↓ + Enter 也走同一个 `dd` click ⇒ 算「真人操作」（第二条派发路径的对照）', async () => {
    const w = await setupSelect()
    const input = w.document.querySelector<HTMLInputElement>(
      '#gameName + .layui-form-select input.layui-input',
    )!
    // ↓（40）：把带 `layui-this` 的 `dd` 挪到下一条（偏移 260600 那段 `prev`/`next`）
    pressKey(w, input, 40)
    expect(currentDd(w)?.getAttribute('lay-value')).toBe('5')
    // Enter（13）：layui 内部是 `g.children("dd."+M).trigger("click")`（偏移 261173）
    // ⇒ 它必须**算**「真人点过」，否则「客服打不开下拉、只用键盘也能选游戏」这一支会漏判。
    pressKey(w, input, 13)
    expect(w.__calls).toEqual(['5'])
    expect((w.document.querySelector('#gameName') as HTMLSelectElement).value).toBe('5')
  })
})
