import { describe, expect, it } from 'vitest'

import { isOrderNoShape } from '../orderNoShape'

/** 合成单号（E-0077：全零段形状、不引真值）。 */
const NO = '10000000000000000002'

describe('m 端单号形状闸（orderNoShape）', () => {
  it('20 位数字 → true', () => {
    expect(isOrderNoShape(NO)).toBe(true)
  })

  it('字母 / 19 位 / 21 位 / 空串 / 带空白 → false（形状不符走 404 态、零请求）', () => {
    expect(isOrderNoShape('abc')).toBe(false)
    expect(isOrderNoShape('1000000000000000001')).toBe(false)
    expect(isOrderNoShape('100000000000000000021')).toBe(false)
    expect(isOrderNoShape('')).toBe(false)
    expect(isOrderNoShape(` ${NO} `)).toBe(false)
  })

  it('非字符串（undefined / 数组）→ false：vue-router params 类型漂移由本函数挡下', () => {
    expect(isOrderNoShape(undefined)).toBe(false)
    expect(isOrderNoShape([NO])).toBe(false)
    expect(isOrderNoShape(null)).toBe(false)
  })
})
