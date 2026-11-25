import { describe, expect, it } from 'vitest'

import { withRawCells } from '../rawCell'

/**
 * `withRawCells()` 的五条判据（spec §3.6-1）。
 *
 * ⚠️ 为什么这些判据必须存在：这一族的**症状是「显示出 HTML 源码」或「`A&B` 变成 `A&amp;B`」**，
 * 而它落在**真 layui 的渲染**里（jsdom 单测不跑真 layui）。所以这里不测「layui 怎么渲染」，
 * 只钉**我们交给它的那个形状**：数据列挂上了原样透传的 `templet`（有 `templet` 的列
 * layui 才原样插入），且它**没有再转义**。
 */
describe('withRawCells：给数据列挂原样 templet', () => {
  it('① 数据列拿到 templet，且 `templet(row)` **逐字等于** `row[field]`（含 `<` / `&` 的值原样出）', () => {
    const html =
      '<div class="orderLine" id="order_X"><a href="javascript:void(0)" style="color:#0094ff">NO1</a></div>'
    const cols: Array<Record<string, unknown>> = [{ field: 'orderIdView', title: '订单号' }]
    const [col] = withRawCells(cols)
    const templet = col!.templet as (row: Record<string, unknown>) => unknown

    expect(typeof templet).toBe('function')
    // 逐字相等：HTML 不被转义（否则用户看到的就是 `<div class="orderLine" …` 这串源码）。
    expect(templet({ orderIdView: html })).toBe(html)
    // ⚠️ 同一格 `A&B` 只该出现一次转义——**这里是零次**：`A&B` 原样透传，不是 `A&amp;B`。
    expect(templet({ orderIdView: 'A&B' })).toBe('A&B')
    // 逐字对拍一条更强的：拿 `escapeHtml` 的产物比，证明它**没有**被二次转义（`&amp;` 不变 `&amp;amp;`）。
    expect(templet({ orderIdView: 'A&amp;B' })).toBe('A&amp;B')
  })

  it('② 列自己已经有 templet ⇒ **保留自己的**，⛔ 不被原样 templet 覆盖', () => {
    const own = (row: Record<string, unknown>): string => `<span>${String(row.v)}</span>`
    const cols = [{ field: 'v', templet: own }]
    const [col] = withRawCells(cols)

    expect(col!.templet).toBe(own)
    expect((col!.templet as typeof own)({ v: 'x' })).toBe('<span>x</span>')
  })

  it('②b 列自己的 `templet` 是**字符串选择器** ⇒ 原样保留（⛔ 不按类型判、不静默换掉）', () => {
    // layui 的 `templet` 合法形态不止函数：字符串是**模板选择器**（先例：`system-sourelist.ts`
    // 的 `'#switchTpl1'`、`customer-customerlist.ts` 的 `'#table_tool'`）。按
    // `typeof === 'function'` 判会把它换成原样透传的 templet ⇒ 那一列从模板 / 开关变成裸文本。
    const cols = [{ field: 'status', title: '状态', templet: '#switchTpl1' }]
    const [col] = withRawCells(cols)

    // 钉**值本身**：仍是同一个字符串（⛔ 不是 `expect.any(Function)`、也不是「不等于空」这种弱判据）。
    expect(col!.templet).toBe('#switchTpl1')
    // 其余键一位不少。
    expect(col).toEqual({ field: 'status', title: '状态', templet: '#switchTpl1' })
  })

  it('③ 没有 `field` 的列（checkbox / 只有 toolbar 的操作列）不挂 templet', () => {
    const cols = [{ type: 'checkbox', fixed: 'left', width: 30 }, { title: '操作', toolbar: '#bar' }]
    const out = withRawCells(cols)

    expect('templet' in out[0]!).toBe(false)
    expect('templet' in out[1]!).toBe(false)
    // 其余键一位不少地留着（`type` / `toolbar` 这些漏了就静默出错）。
    expect(out[0]).toEqual({ type: 'checkbox', fixed: 'left', width: 30 })
    expect(out[1]).toEqual({ title: '操作', toolbar: '#bar' })
  })

  it('③b `field` 是空串或非字符串 ⇒ 同样不挂（空 field 的 templet 只会取到 `undefined`）', () => {
    const out = withRawCells([{ field: '', title: '空' }, { field: 1, title: '数字' }])

    expect('templet' in out[0]!).toBe(false)
    expect('templet' in out[1]!).toBe(false)
  })

  it('④ 返回**新数组 + 新列对象**：入参 `cols` 与新数组里的列对象都不被改（引用与内容都断）', () => {
    const cols = [{ field: 'a', title: '甲' }, { field: 'b', title: '乙', templet: () => 'x' }]
    const snapshot = JSON.parse(JSON.stringify(cols)) as unknown

    const out = withRawCells(cols)

    expect(out).not.toBe(cols)
    expect(out[0]).not.toBe(cols[0])
    expect(out[1]).not.toBe(cols[1])
    // 内容相等：原对象一个键都没变（`templet` 只出现在**返回值**里）。
    expect(JSON.parse(JSON.stringify(cols))).toEqual(snapshot)
    expect('templet' in (cols[0] as object)).toBe(false)
    expect(out[0]).toEqual({ field: 'a', title: '甲', templet: expect.any(Function) })
  })

  it('⑤ `null` / 缺键 ⇒ 空串（⛔ 不把字面量 `undefined` / `null` 拼进单元格）；`0` **原样**透传', () => {
    const [col] = withRawCells([{ field: 'a' }] as Array<Record<string, unknown>>)
    const templet = col!.templet as (row: Record<string, unknown>) => unknown

    expect(templet({ a: null })).toBe('')
    expect(templet({})).toBe('')
    expect(templet({ a: undefined })).toBe('')
    // ⚠️ E-0021：`0` 是**有值**，不许用真值判断把它当「没有」（`|| ''` 会吞掉它）。
    expect(templet({ a: 0 })).toBe(0)
  })
})
