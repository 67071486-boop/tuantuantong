// 纯逻辑测试，不依赖 DOM（vitest 全局默认 node 环境）。
import { describe, expect, it } from 'vitest'
import { formatDateTime } from '../datetime'

/**
 * 三态判据（任务书 T3）：`+08:00` 串、`+00:00` 串、非法串。
 * 背景见 `datetime.ts` 文件头：两族时区口径并存，判据必须按串内偏移换算。
 */
describe('m/utils/datetime::formatDateTime', () => {
  it('+08:00 串（DRF 项目时区序列化）＝ 墙钟原样；只取到分钟、吃掉秒与小数秒', () => {
    expect(formatDateTime('2026-10-10T08:05:00+08:00')).toBe('2026-10-10 08:05')
    // DRF 实际会带 microseconds（T1 评审更正注记里的形状）。
    expect(formatDateTime('2026-10-10T08:05:07.110591+08:00')).toBe('2026-10-10 08:05')
  })

  it('+00:00 串（UTC）＝ +8 小时换算；Z 后缀同 UTC；跨日进位正确', () => {
    expect(formatDateTime('2026-10-10T00:30:00+00:00')).toBe('2026-10-10 08:30')
    expect(formatDateTime('2026-10-10T16:00:00Z')).toBe('2026-10-11 00:00')
    // 月末跨月（UTC 10-31 20:00 + 8h = 11-01 04:00）。
    expect(formatDateTime('2026-10-31T20:00:00+00:00')).toBe('2026-11-01 04:00')
    // 负偏移的串同样按它自己的偏移换算（-05:00 → +13 小时到东八）。
    expect(formatDateTime('2026-10-10T19:30:00-05:00')).toBe('2026-10-11 08:30')
  })

  it('无偏移的串按「已是项目时区墙钟」原样取；非法输入一律空串（不回显原始串）', () => {
    expect(formatDateTime('2026-10-10T08:05:00')).toBe('2026-10-10 08:05')
    expect(formatDateTime('2026-10-10 08:05:00')).toBe('2026-10-10 08:05')
    expect(formatDateTime('not-a-date')).toBe('')
    expect(formatDateTime('')).toBe('')
    expect(formatDateTime('2026-10-10')).toBe('')
    expect(formatDateTime(null)).toBe('')
    expect(formatDateTime(undefined)).toBe('')
    expect(formatDateTime(123)).toBe('')
  })
})
