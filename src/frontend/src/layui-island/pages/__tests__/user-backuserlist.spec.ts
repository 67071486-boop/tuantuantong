import { describe, expect, it } from 'vitest'
import { COLS, PAGE_LIMITS, TABLE } from '../user-backuserlist'

/** `ArtifactCol` 之外的键（`type` 等）在断言里统一窄化。 */
type Extra = { type?: string }

describe('系统用户黑名单管理（/User/BackUserList）列声明', () => {
  it('12 列，文案序列与 BackUserList.js L36-75 的 cols[0] 一致', () => {
    expect(COLS.length).toBe(12)
    expect(COLS.map((c) => c.title)).toEqual([
      undefined,
      '手机号',
      '微信号',
      'QQ',
      '名称',
      '身份证号',
      'YY号',
      '收款账号',
      '原因或备注',
      '加入时间',
      '添加人',
      '操作',
    ])
  })

  it('勾选列带 type:checkbox（缺它会静默丢列，探针不会报）', () => {
    // 出处：`BackUserList.js` L37 `{ type: 'checkbox', fixed: 'left', width: 30 }`。
    expect((COLS[0] as unknown as Extra).type).toBe('checkbox')
    expect(COLS[0]?.fixed).toBe('left')
  })

  it('「手机号」是本页唯一的 fixed:left 业务列，且 align:left（L39）', () => {
    expect(COLS[1]?.fixed).toBe('left')
    expect(COLS[1]?.align).toBe('left')
    expect(COLS[1]?.width).toBe(150)
    expect(COLS.filter((c) => c.fixed === 'left').length).toBe(2) // 勾选列 + 手机号
  })

  it('「原因或备注」没有 width（L58）⇒ 自动列（实测对标渲染宽 338）', () => {
    expect(COLS[8]?.width).toBeUndefined()
    expect(COLS[8]?.align).toBe('left')
  })

  it('操作列 width 80 + toolbar #baroperating + unresize（L75）', () => {
    expect(COLS[11]?.width).toBe(80)
    expect(COLS[11]?.toolbar).toBe('#baroperating')
    expect(COLS[11]?.unresize).toBe(true)
  })

  it('分页：limit 20、7 档 limits（L77-79）；defaultToolbar 取后写的 filter（L33/L35）', () => {
    expect(PAGE_LIMITS).toEqual([10, 20, 30, 40, 50, 80, 100])
    expect(TABLE.page).toEqual({ limit: 20, limits: PAGE_LIMITS })
    expect(TABLE.defaultToolbar).toEqual(['filter'])
    expect(TABLE.toolbar).toBe('#toolbarDemo')
  })
})
