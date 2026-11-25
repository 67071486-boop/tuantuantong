/**
 * 给「单元格值已经是拼好的 HTML」的列补一个**原样透传**的 `templet`。
 *
 * ## 为什么需要它（读码事实，⛔ 不是猜测）
 *
 * vendored 的 layui 2.13.9（`public/layui/layui.js`，按**字节偏移**读）：
 *
 * - 偏移 **279727**：`table` 的原型配置里写着 `escape:!0`
 *   （`a.prototype.config={limit:10,loading:!0,escape:!0,cellMinWidth:60,…}`）；
 * - 偏移 **272879**：单元格取值路径是 `("escape"in a?a:t).escape&&(i=m.escape(i))`
 *   —— `a` 是列对象、`t` 是表配置；**列上没有 `escape` 键时取表配置** ⇒ 默认**转义**；
 * - 同一个函数紧接着 `t=e.text&&a.exportTemplet||a.templet||a.toolbar`：
 *   **挂了 `templet` 的列**由 templet 的返回值**原样**使用 ⇒ 上面那个转义分支被覆盖。
 *
 * ⇒ 本仓那些「行映射（`order-orderrecycle.ts::buildRecycleRow` /
 * `order-tbsendrecordlist.ts::buildSendRow`）已经把单元格拼成 HTML」的页面，列上**必须**
 * 挂 `templet`：不挂 ⇒ 用户看到的是 HTML 源码（回收站页的现象），或者值被**二次转义**
 * （`A&B` → `A&amp;B`，发货记录页的现象）。
 *
 * ## ⚠️ 它**不是**转义层（本模块成立的**前提**）
 *
 * `withRawCells()` 把值**原样**交给 layui，⛔ 不做任何转义 / 加工 ⇒ 它成立的前提是
 * **调用方的行映射已经对来自请求数据的字面做过 `escapeHtml`**。
 * ⛔ 不要把它当 XSS 防线用：给一个装着未转义用户输入的列挂上本 templet，
 * 等于把那个输入直接 `innerHTML` 进页面（存储型 XSS）。
 *
 * ## 为什么按 `field` 记忆化
 *
 * 同一个 `field` 只建一个 templet 函数。理由不是省那点分配，而是让**派生两次**的渲染列
 * 在结构上全等：页面调用点那几条断言钉的是**真 `table.render` 的入参**
 * （`expect(opts.cols[0]).toEqual(buildRenderCols())`）。每次造新闭包的话，`toEqual`
 * 会因函数引用不同而红 ⇒ 只能把它们放宽成 `expect.any(Function)`，而那会把
 * 「这一列真的挂了 templet、且挂的是哪个」这条判据磨钝（`AGENTS.md` §5-G：既有断言不放宽）。
 */

/** 记忆化表：`field` → 该列的原样 templet（只依赖 `field` 一个变数，故可共用）。 */
const rawTemplets = new Map<string, (row: Record<string, unknown>) => unknown>()

function rawTempletFor(field: string): (row: Record<string, unknown>) => unknown {
  let templet = rawTemplets.get(field)
  if (templet === undefined) {
    // `row[field] ?? ''`：缺键 / `null` 回空串（⛔ 不把字面量 `undefined` 拼进单元格）；
    // ⚠️ 用 `??` 而不是 `||`——`0` 是**有值**，真值判断会把它吞掉（E-0021）。
    templet = (row) => row[field] ?? ''
    rawTemplets.set(field, templet)
  }
  return templet
}

/**
 * 返回一份**新**的列定义：每个「有非空字符串 `field`、且自己没有 `templet` **键**」的列
 * 补上原样透传的 `templet`；其余列（已有 `templet` 的、`type:'checkbox'` / 只有
 * `toolbar` 的操作列）原样保留自己的键。
 *
 * ⚠️ 「有没有 `templet`」按**键存在**判（`'templet' in col`），⛔ **不按值的类型判**：
 * layui 的 `templet` 合法形态不止函数——**字符串选择器**同样是合法形态（先例：
 * `system-sourelist.ts` 的 `'#switchTpl1'`、`customer-customerlist.ts` 的 `'#table_tool'`）。
 * 按 `typeof === 'function'` 判会把**字符串形态的列静默换掉**：那一列从模板 / 开关
 * 退化成裸文本（值本身还在，所以不报错、只是显示错——正是本模块要修的那类静默缺陷）。
 *
 * ⛔ 入参 `cols` 与新数组里的列对象都**不被改动**（`buildXxxCols()` 的返回值是证据件
 * 对拍对象、被多处读）。
 */
export function withRawCells<T extends Record<string, unknown>>(cols: readonly T[]): T[] {
  return cols.map((col): T => {
    // 列自己已经有 `templet` **键** ⇒ 保留自己的（先例：发货记录页 `SendResult` 的
    // `resultTemplet`；字符串形态如 `'#switchTpl1'` 也算——⛔ 不按类型判）。
    if ('templet' in col) return { ...col }
    const field = col.field
    // 没有 `field` 的列（勾选列 / 操作列）：挂 templet 只会取到 `undefined`。
    if (typeof field !== 'string' || field === '') return { ...col }
    return { ...col, templet: rawTempletFor(field) }
  })
}
