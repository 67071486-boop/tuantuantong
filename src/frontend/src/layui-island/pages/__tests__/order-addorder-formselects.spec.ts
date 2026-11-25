/**
 * 「隐藏 `<select>` 同步」的**契约**测试：用**真 vendored layui 2.13.9 + 真 `public/vendor/formselects-v4.js`**
 * 在独立 JSDOM 里把插件的事件时序钉住（T6 评审 C1 的根因）。
 *
 * 为什么值得单独一条：`order-addorder.ts` 里那个同步回调必须注册在 **`endOn`**（`on` 的第三参 `true`）上，
 * 因为 vendored 插件的顺序是
 *   `on(id, vals.concat([]))`（**改前**）→ 改 `data[id].values` → `endOn(id, vals.concat([]))`（**改后**）
 * （`formselects-v4.js:1177`（`on` 调用）/ `:1182`（`vals.push`）/ `:1196`（`endOn` 调用）；
 * radio 的 `addLabel`（`:1199`）在 `:1209-1213` 先 `values.length=0` 再 `push`；
 * radio 的快捷按钮只有「清空」，`:169` / `:113`）。
 * 用 `on` ⇒ 写进 `<select>` 的是**上一个** id ⇒ 编辑/录单把来源 A 改成 B 时发出去的是 A。
 *
 * 本用例只钉**插件**的时序（回调只记录参数，不含业务逻辑）——于是它与被测页面互不依赖，
 * 插件升级后若语义变了，这条会先红，提示依赖它的替身与页面同步更新。
 *
 * ⚠️ **回调必须在 JSDOM 的 realm 里创建**（这里走 `window.eval`）：跨 realm 的函数**根本注册不进去**——
 * 第一道门是 `bindEvent` 的 `fun instanceof Function`（`:1467`，不成立就整条回调不登记），
 * `handlerLabel` 里的 `on instanceof Function`（`:1177`）因此**到不了**（实测踩到）。
 * 这也正是「本文件不能用它直接驱动被测模块」的原因：被测模块的回调在 vitest 的 realm 里。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { JSDOM } from 'jsdom'
import { describe, expect, it } from 'vitest'

const LAYUI_JS = path.resolve(process.cwd(), 'public/layui/layui.js')
const FORMSELECTS_JS = path.resolve(process.cwd(), 'public/vendor/formselects-v4.js')

type FsEvent = ['on' | 'endOn', number, unknown]

type ProbeWindow = JSDOM['window'] & {
  eval: (src: string) => unknown
  __events: FsEvent[]
}

const HTML = `
  <select name="orderSoures" id="orderSoures" xm-select-radio lay-search="" xm-select="orderSoures"
    xm-select-search="" xm-select-search-type="title">
    <option value="20">闲鱼A</option><option value="11">淘宝店</option>
  </select>`

/** 真 layui + 真 formSelects（同一个 JSDOM；`runScripts: 'dangerously'` 才能执行内联脚本）。 */
function setupRealPlugin(): ProbeWindow {
  const dom = new JSDOM(`<!doctype html><html><body>${HTML}</body></html>`, {
    runScripts: 'dangerously',
    url: 'http://localhost/',
  })
  const w = dom.window as unknown as ProbeWindow
  for (const file of [LAYUI_JS, FORMSELECTS_JS]) {
    const el = w.document.createElement('script')
    el.textContent = readFileSync(file, 'utf8')
    w.document.body.appendChild(el)
  }
  // 真插件已渲染出 `dl[xid]`；把两个钩子的**参数**记下来（回调在 JSDOM realm 里定义）
  w.eval(`
    window.__events = [];
    var rec = function (kind) {
      return function (_id, vals) {
        window.__events.push([kind, vals.length, vals.length ? vals[0].value : null]);
      };
    };
    window.layui.formSelects.on('orderSoures', rec('on'));
    window.layui.formSelects.on('orderSoures', rec('endOn'), true);
    window.__pick = function (val) {
      document.querySelector('dl[xid="orderSoures"] dd[lay-value="' + val + '"]').click();
    };
    window.__clear = function () {
      var dl = document.querySelector('dl[xid="orderSoures"]');
      var btn = (dl.closest('.xm-select-parent') || document).querySelector('i.icon-qingkong');
      if (btn) btn.click();
    };
  `)
  return w
}

describe('真 vendored formselects：单选（xm-select-radio）的事件时序', () => {
  it('点选 ⇒ `on` 拿的是**改前**值、`endOn` 拿的是**改后**值（所以同步必须挂 endOn）', () => {
    const w = setupRealPlugin()
    expect(w.document.querySelector('dl[xid="orderSoures"]')).not.toBeNull()

    w.eval('__pick("11")')
    // 改前是空、改后是 11：这就是「挂 on 会写上一个 id」的机制（首点时 on 的 vals 为空）
    expect(w.__events).toEqual([
      ['on', 0, null],
      ['endOn', 1, '11'],
    ])

    w.__events = []
    w.eval('__pick("20")')
    // 改前是 11、改后是 20：挂 `on` 会把 11 写进隐藏 select（C1 的第二处交互）
    expect(w.__events).toEqual([
      ['on', 1, '11'],
      ['endOn', 1, '20'],
    ])
  })

  it('点「清空」⇒ `endOn` 拿到**空集**（同步必须处理空集，否则 select 留着旧 id）', () => {
    const w = setupRealPlugin()
    w.eval('__pick("11")')
    w.__events = []
    w.eval('__clear()')
    expect(w.__events).toEqual([
      ['on', 1, '11'],
      ['endOn', 0, null],
    ])
  })

  // T6 二轮复评 NEW-I1 依赖的两条真插件行为——一条用例钉死（页面替身照它对齐）：
  //  ① API 写入**不触发**任何钩子（`handlerLabel(..., notOn=true)`，`:1563` / `:1568` 第 5 参
  //     ⇒ `:1176` 的 `if (!notOn)` 与 `:1196` 的 `!notOn && endOn…` 都不成立）——「回填/复位不会把
  //     『用户清过来源』标志误置」正是靠这条。
  //  ② API 写入**加不进选项里没有的 id**（`:1562` 的 `dd[lay-value]` 命中；`:1566` 的 `valToName`
  //     对非联动的本地字典取不到名字 ⇒ 空串 ⇒ `if (name)` 不成立）——字典 `[20,11]` 里没有 999 时，
  //     `value('orderSoures', [999], true)` 之后插件里仍是空的。
  it('API 写入 `value(id, [ids], true)`：不触发钩子，且加不进选项里没有的 id（NEW-I1 的两条前提）', () => {
    const w = setupRealPlugin()
    const val = (): string => String(w.eval('JSON.stringify(window.layui.formSelects.value("orderSoures", "val"))'))
    expect(val()).toBe('[]')

    w.eval('window.layui.formSelects.value("orderSoures", [999], true)')
    expect(val()).toBe('[]')
    expect(w.__events).toEqual([])

    w.eval('window.layui.formSelects.value("orderSoures", [11], true)')
    expect(val()).toBe('["11"]')
    expect(w.__events).toEqual([])
  })
})
