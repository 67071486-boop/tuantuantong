import { describe, expect, it } from 'vitest'
import { COLS, PAGE_LIMITS, TABLE } from '../user-usercontractsigning'

/** `ArtifactCol` 之外的键（`type` 等）在断言里统一窄化。 */
type Extra = { type?: string }

describe('签约管理（/User/UserContractSigning）列声明', () => {
  it('10 列，文案序列与 UserContractSigning.js L67-157 的 cols[0] 一致', () => {
    expect(COLS.length).toBe(10)
    expect(COLS.map((c) => c.title)).toEqual([
      undefined,
      '真实姓名',
      '身份证号',
      '手机号',
      '系统用户名称',
      '年龄',
      '签约状态',
      '邀请时间',
      '签约时间',
      '操作',
    ])
  })

  it('勾选列带 type:checkbox（缺它会静默丢列，探针不会报）', () => {
    // 出处：`UserContractSigning.js` L68 `{ type: 'checkbox', fixed: 'left', width: 30 }`。
    expect((COLS[0] as unknown as Extra).type).toBe('checkbox')
    expect(COLS[0]?.fixed).toBe('left')
  })

  it('最后一列是 templet（不是 toolbar）⇒ **不要**写 toolbar（本页没有 #baroperating）', () => {
    // 出处：L131-156 是 `templet: function(res){…}`，按行状态拼按钮。
    expect(COLS[9]?.width).toBe(240)
    expect(COLS[9]?.align).toBe('left')
    expect(COLS[9]?.toolbar).toBeUndefined()
    expect(COLS.some((c) => c.toolbar !== undefined)).toBe(false)
  })

  it('末两列 field 同名都是 sigingCompleteTime（L128/L131，照抄不"修好"）', () => {
    expect(COLS[8]?.field).toBe('sigingCompleteTime')
    expect(COLS[9]?.field).toBe('sigingCompleteTime')
  })

  it('本页一处 unresize:true 都没有（L67-157）', () => {
    expect(COLS.some((c) => c.unresize === true)).toBe(false)
  })

  it('分页：limit 20、3 档 limits（L159-161）；defaultToolbar 取后写的 filter（L64/L66）', () => {
    expect(PAGE_LIMITS).toEqual([20, 50, 100])
    expect(TABLE.page).toEqual({ limit: 20, limits: PAGE_LIMITS })
    expect(TABLE.defaultToolbar).toEqual(['filter'])
    expect(TABLE.toolbar).toBe('#toolbarDemo')
  })
})
