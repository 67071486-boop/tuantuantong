import { describe, expect, it } from 'vitest'
import { COLS, PAGE_LIMITS, TABLE } from '../gameaccounts-myaccountlist'

describe('指定给我的账号 layui 页的列声明', () => {
  it('勾选列带 type:checkbox（缺它会静默变成普通列，探针不会报）', () => {
    expect((COLS[0] as unknown as { type?: string }).type).toBe('checkbox')
    expect(COLS[0]?.width).toBe(30)
    expect(COLS[0]?.fixed).toBe('left')
  })

  it('17 列（勾选 + 16 个数据列），文案序列与对标 myaccountlist.js 的 cols[0] 一致', () => {
    expect(COLS.length).toBe(17)
    expect(COLS.slice(1).map((c) => c.title)).toEqual([
      '账号类型',
      '角色名',
      '阵营',
      '角色信息',
      '游戏账号',
      '子账号',
      '账号状态',
      '解封时间',
      '用途类型',
      '进度状态',
      '最后排团时间',
      '接手人',
      '入库时间',
      '账号截图',
      '备注',
      '机位',
    ])
  })

  it('15 个百分比列宽按对标**原样透传**（不换算成像素），只有「账号截图」是字面量', () => {
    // 出处：`myaccountlist.js` 的 15 处 `width: isPc ? '<N>%' : <px>` 取桌面分支（isPc = true），
    // 与唯一的字面量 `{ field:'img', …, width: 80 }`。**不写成像素**：人工换算会让 x 坐标逐列漂 1–4px
    // （实测 230/296/509 vs 232/299/513），透传才能与对标用同一份 layui 算法算同一份输入。
    const pct = COLS.filter((c) => typeof c.width === 'string').map((c) => `${c.field}=${String(c.width)}`)
    expect(pct).toEqual([
      'accType=5%',
      'gameRoleName=7%',
      'gameGroup=4%',
      'gameInfo=13%',
      'gameAcc=9%',
      'zgameAcc=4%',
      'accStatus=5%',
      'tempDisableTime=5%',
      'accUseType=7%',
      'proStatus=4%',
      'orderSoure=6%',
      'recUserName=5%',
      'createTime=5%',
      'remark=7%',
      'jiwei=7%',
    ])
    // 唯一的字面量列宽
    expect(COLS.find((c) => c.field === 'img')?.width).toBe(80)
  })

  it('分页：5 档 limits 取自该页 JS；limit 20', () => {
    // 出处：`myaccountlist.js` 的 `, page: true` / `, limit: 20` / `, limits: [10, 20, 30, 40, 50]`。
    expect(PAGE_LIMITS).toEqual([10, 20, 30, 40, 50])
    expect(TABLE.page).toEqual({ limit: 20, limits: PAGE_LIMITS })
  })

  it('对标同对象里写了两次 defaultToolbar，本页按生效的 filter 传', () => {
    expect(TABLE.defaultToolbar).toEqual(['filter'])
  })

  it('工具栏/表格选择器与对标同值', () => {
    expect(TABLE.elem).toBe('#tableList')
    expect(TABLE.toolbar).toBe('#toolbarDemo')
  })
})
