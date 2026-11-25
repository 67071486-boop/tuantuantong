import { describe, expect, it } from 'vitest'
import { COLS, PAGE_LIMITS, TABLE } from '../user-userloginrestrict'

/** `ArtifactCol` 之外的键（`type` 等）在断言里统一窄化。 */
type Extra = { type?: string }

describe('用户登录限制设置（/User/userloginrestrict）列声明', () => {
  it('11 列，文案序列与 UserLoginRestrict.js L43-111 的 cols[0] 一致', () => {
    expect(COLS.length).toBe(11)
    expect(COLS.map((c) => c.title)).toEqual([
      undefined,
      '系统用户',
      '限制周期段',
      '限制时间段',
      '限制方式',
      'IP范围',
      // 第 7 列的 `title` 原文里带着对标自己的 `<span lay-tips="…">`（L100），逐字抄。
      '<span lay-tips="绿色代表这个规则状态开启, 符合规则条件的用户才能登录。">规则状态</span>',
      '创建时间',
      '更新时间',
      '修改人名称',
      '操作',
    ])
  })

  it('勾选列带 type:checkbox（缺它会静默丢列，探针不会报）', () => {
    // 出处：`UserLoginRestrict.js` L44 `{ type: 'checkbox', fixed: 'left', width: 30 }`。
    expect((COLS[0] as unknown as Extra).type).toBe('checkbox')
    expect(COLS[0]?.fixed).toBe('left')
    expect(COLS[0]?.width).toBe(30)
  })

  it('「IP范围」写的是 minwidth（小写，无效）⇒ **既没有 width 也没有 minWidth**，是自动列', () => {
    // 出处：`UserLoginRestrict.js` L97 `{ field:'IPWhitelist', title:'IP范围', minwidth: 120, … }`。
    // layui 只认 `minWidth`；「修好」成 minWidth 会把这条自动列钉成定宽列（实测对标渲染宽 374）。
    expect(COLS[5]?.title).toBe('IP范围')
    expect(COLS[5]?.width).toBeUndefined()
    expect(COLS[5]?.minWidth).toBeUndefined()
  })

  it('「规则状态」列宽 75 + switchText「生效|禁用」（对标是 templet:"#switchTpl1"）', () => {
    expect(COLS[6]?.width).toBe(75)
    expect(COLS[6]?.switchText).toBe('生效|禁用')
  })

  it('操作列 width 85 + toolbar #baroperating + unresize（L111）', () => {
    expect(COLS[10]?.width).toBe(85)
    expect(COLS[10]?.toolbar).toBe('#baroperating')
    expect(COLS[10]?.unresize).toBe(true)
  })

  it('分页：limit 50、5 档 limits（L113-115）；defaultToolbar 取后写的 filter（L40/L42）', () => {
    expect(PAGE_LIMITS).toEqual([10, 20, 30, 40, 50])
    expect(TABLE.page).toEqual({ limit: 50, limits: PAGE_LIMITS })
    expect(TABLE.defaultToolbar).toEqual(['filter'])
    expect(TABLE.toolbar).toBe('#toolbarDemo')
  })
})
