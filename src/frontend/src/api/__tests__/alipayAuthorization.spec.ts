import { describe, expect, it, vi } from 'vitest'

import { alipayStatusLabel, newAlipayRequestId } from '../alipayAuthorization'

describe('支付宝授权辅助方法', () => {
  it('在没有 randomUUID 的环境中生成安全的 UUID', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => { bytes.fill(1); return bytes })
    vi.stubGlobal('crypto', { getRandomValues })
    try {
      expect(newAlipayRequestId()).toBe('01010101-0101-4101-8101-010101010101')
      expect(getRandomValues).toHaveBeenCalledOnce()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('已授权但最新状态不确定时不显示授权成功', () => {
    expect(alipayStatusLabel('authorized', '暂无法确认最新授权状态')).toBe('状态待确认')
    expect(alipayStatusLabel('authorized', null)).toBe('授权成功')
    expect(alipayStatusLabel('signed', null)).toBe('签约成功，打款待确认')
    expect(alipayStatusLabel('signed', '操作结果待核对')).toBe('状态待确认')
    expect(alipayStatusLabel('signed', null, true)).toBe('状态待确认')
  })
})
