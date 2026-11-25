import { describe, expect, it } from 'vitest'
import { COLS, TABLE } from '../user-usergroup'

/** `ArtifactCol` 之外的键（`type` 等）在断言里统一窄化。 */
type Extra = { type?: string }

describe('角色职位分组（/User/usergroup）列声明', () => {
  it('3 列，文案序列与 usergroup.js L24-38 的 cols[0] 一致', () => {
    expect(COLS.length).toBe(3)
    expect(COLS.map((c) => c.title)).toEqual(['角色分组名称', '包含具体角色职位', '操作'])
  })

  it('无勾选列（对标 cols 里没有 type:checkbox）', () => {
    expect(COLS.some((c) => (c as unknown as Extra).type !== undefined)).toBe(false)
  })

  it('「包含具体角色职位」没有 width（L32）⇒ 自动列，由 layui 均分剩余宽度', () => {
    expect(COLS[1]?.width).toBeUndefined()
    expect(COLS[1]?.align).toBe('left')
    expect(COLS[1]?.unresize).toBe(true)
  })

  it('操作列 width 130 + toolbar #baroperating（L37，漏 toolbar 会静默变空列）', () => {
    expect(COLS[2]?.width).toBe(130)
    expect(COLS[2]?.toolbar).toBe('#baroperating')
    // 对标写 `fixed: ''`（= 不固定，layui 默认）⇒ 我们不写该键。
    expect(COLS[2]?.fixed).toBeUndefined()
  })

  it('page:false（L39）、defaultToolbar:[]（L23，本页只写一次）、height:"full"（L21）', () => {
    expect(TABLE.page).toBe(false)
    expect(TABLE.defaultToolbar).toEqual([])
    expect(TABLE.height).toBe('full')
    // 本页没有工具栏（`table.render` 里没有 `toolbar` 键）。
    expect(TABLE.toolbar).toBeUndefined()
  })
})
