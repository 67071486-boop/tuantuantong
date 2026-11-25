import { describe, expect, it } from 'vitest'
import { COLS, PAGE_TITLE, TABLE } from '../emp-funddetailslist'

describe('团团通资金明细与提现（对标原文「鸡蛋铺资金明细与提现」，/Emp/funddetailslist）列与渲染参数声明', () => {
  // ⭐（D-0339）业务方指令改名「鸡蛋铺资金明细与提现 → 团团通资金明细与提现」
  //（只动显示层：菜单文案 + 页面标题）。钉 **值**（⛔ 不从页面模块派生）：防止有人「照对标改回去」
  // 或把页面标题与菜单名改岔。对标 `<title>`/菜单名原文照旧是「鸡蛋铺资金明细与提现」（有意偏离，
  // 登记见页面模块 `PAGE_TITLE` 注与 `config/replicaRoutes.ts` 该条 note）。
  it('页面标题是改名后的我方名（对标原文「鸡蛋铺资金明细与提现」只作备查，不渲染）', () => {
    expect(PAGE_TITLE).toBe('团团通资金明细与提现')
  })

  it('7 列，文案序列与 funddetailslistmang.js L48-74 一致', () => {
    expect(COLS.length).toBe(7)
    expect(COLS.map((c) => c.title)).toEqual([
      '支付流水号',
      '相关订单号',
      '资金方向',
      '金额',
      '可用余额',
      '时间',
      '备注说明',
    ])
  })

  it('第 5 列 field 是对标原文的拼写 Banlance（少一个 a，别"修好"）', () => {
    expect(COLS[4]?.field).toBe('Banlance')
    expect(COLS[4]?.title).toBe('可用余额')
  })

  it('备注说明列无 width（自动列，实测渲染 883px）且 align:center', () => {
    expect(COLS[6]?.field).toBe('Remark')
    expect(COLS[6]?.width).toBeUndefined()
    expect(COLS[6]?.align).toBe('center')
  })

  it('列宽全是纯数字、无 `isPc ?` 三元（本页 isPc 只用于弹窗 area）', () => {
    expect(COLS.map((c) => c.width)).toEqual([200, 200, 60, 80, 80, 140, undefined])
    expect(COLS.every((c) => c.fixed === undefined && c.sort === undefined)).toBe(true)
  })

  it('table 参数：limits 六项且从 20 起（L77）、defaultToolbar 取后写生效的 ["filter","exports"]（L47）', () => {
    expect(TABLE.elem).toBe('#tableList')
    expect(TABLE.toolbar).toBe('#toolbarDemo')
    expect(TABLE.defaultToolbar).toEqual(['filter', 'exports'])
    expect(TABLE.page).toEqual({ limit: 20, limits: [20, 50, 100, 200, 300, 500] })
  })
})
