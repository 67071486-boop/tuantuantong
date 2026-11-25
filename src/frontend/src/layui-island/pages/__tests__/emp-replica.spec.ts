import { describe, expect, it } from 'vitest'
import { COLS as ALIPAY_COLS, PAGE_LIMITS as ALIPAY_LIMITS, TABLE as ALIPAY_TABLE } from '../emp-bindalipaysettauth'

/**
 * `ArtifactCol` 之外的键（`type`/`edit` 等）在断言里统一窄化。
 *
 * 2026-09-21 业务裁剪（D-0036）删掉「内部学习资料」（`emp-learminformationlist`）与
 * 「虚拟号码通话记录」（`emp-privatephonecalllog`）两页及其两个 describe，
 * 本文件只剩「绑定用于结算打款的支付宝管理」一页。
 */
type Extra = { type?: string }

describe('绑定用于结算打款的支付宝管理列表（/Emp/bindalipaysettauth）列声明', () => {
  it('13 列、**无勾选列**（对标 cols 里没有 type:checkbox）', () => {
    // 出处：`BindAlipaySettAuth.js` L24-105（第 1 列就是 field:'str1'）。
    expect(ALIPAY_COLS.length).toBe(13)
    expect(ALIPAY_COLS.some((c) => (c as unknown as Extra).type !== undefined)).toBe(false)
    expect(ALIPAY_COLS.map((c) => c.title)).toEqual([
      '付款支付宝账号',
      '支付宝名称',
      '通道',
      '服务签约状态',
      '功能授权状态',
      '申请时间',
      '授权时间',
      '备注说明',
      '解除授权时间',
      '解除授权说明',
      '系统提示',
      '操作人',
      '操作',
    ])
  })

  it('「备注说明」带 edit:true（L81，空表下探针看不见，漏了是静默差异）', () => {
    expect(ALIPAY_COLS[7]?.edit).toBe(true)
  })

  it('操作列 width 210 + toolbar #baroperating（L104）', () => {
    expect(ALIPAY_COLS[12]?.width).toBe(210)
    expect(ALIPAY_COLS[12]?.toolbar).toBe('#baroperating')
  })

  it('page:false（分页关闭，L106）；limit/limits 照本页 JS 原样带上', () => {
    expect(ALIPAY_TABLE.page).toBe(false)
    expect(ALIPAY_LIMITS).toEqual([10, 20, 30, 40, 50, 100])
    expect(ALIPAY_TABLE.limit).toBe(50)
    expect(ALIPAY_TABLE.pageLimits).toEqual(ALIPAY_LIMITS)
    expect(ALIPAY_TABLE.cellMinWidth).toBeUndefined()
    expect(ALIPAY_TABLE.defaultToolbar).toEqual(['filter'])
  })
})
