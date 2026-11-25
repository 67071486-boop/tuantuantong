import { describe, expect, it } from 'vitest'
import { M_SITE_BASE, mRoutePathOf, mSitePath, sanitizeMRedirect } from '../redirect'

/**
 * 手机端 `redirect` 清洗的纯函数判据（任务书 T0 逐条点名 + 桌面 `safe-redirect.spec` 同款攻击面）。
 * 恰好集合断言：**合法输入的全集**在「放行」那条里枚举（⛔ 不用 toContain 放宽）。
 */
describe('sanitizeMRedirect', () => {
  it('合法的 /m/ 站内路径恰有三类：带 query 的深链 / 不带 query 的页 / m 首页本身', () => {
    expect(sanitizeMRedirect('/m/hall?game=3')).toBe('/m/hall?game=3')
    expect(sanitizeMRedirect('/m/mine')).toBe('/m/mine')
    expect(sanitizeMRedirect('/m/')).toBe('/m/')
  })

  it('外站 URL 与协议相对形态一律拒', () => {
    expect(sanitizeMRedirect('https://evil.example')).toBeNull()
    expect(sanitizeMRedirect('//evil.com')).toBeNull()
    expect(sanitizeMRedirect('/\\evil.com')).toBeNull()
  })

  it('桌面路径一律拒（m 端路由表里没有这些目标）', () => {
    expect(sanitizeMRedirect('/orders')).toBeNull()
    expect(sanitizeMRedirect('/login')).toBeNull()
    expect(sanitizeMRedirect('/')).toBeNull()
    expect(sanitizeMRedirect('/m')).toBeNull() // 不带尾斜杠的不是 /m/ 前缀
  })

  it('反斜杠把戏：前缀外（/m\\…）与前缀内（含编码还原）都拒', () => {
    expect(sanitizeMRedirect('/m\\evil')).toBeNull()
    expect(sanitizeMRedirect('/m/\\evil')).toBeNull()
    expect(sanitizeMRedirect('/m/%5cevil')).toBeNull() // decodeOnce 还原成 /m/\evil
  })

  it('相对段爬出 /m/ 的（. / ..）一律拒', () => {
    expect(sanitizeMRedirect('/m/../..')).toBeNull()
    expect(sanitizeMRedirect('/m/..')).toBeNull()
    expect(sanitizeMRedirect('/m/./hall')).toBeNull()
    expect(sanitizeMRedirect('/m/hall/../mine')).toBeNull()
  })

  it('前缀后再塞斜杠的（/m//evil.com，含编码形态）拒', () => {
    expect(sanitizeMRedirect('/m//evil.com')).toBeNull()
    expect(sanitizeMRedirect('/m/%2f%2fevil.com')).toBeNull() // decodeOnce → /m///evil.com
  })

  it('控制字符（TAB/CR/LF）拒', () => {
    expect(sanitizeMRedirect('/m/\tevil')).toBeNull()
    expect(sanitizeMRedirect('/m/ha\rll')).toBeNull()
  })

  it('非字符串（null / undefined / 数组 query 形态）拒', () => {
    expect(sanitizeMRedirect(null)).toBeNull()
    expect(sanitizeMRedirect(undefined)).toBeNull()
    expect(sanitizeMRedirect(['/m/hall'])).toBeNull()
  })

  it('双反斜杠编码形态不放大成异常（裸 % 解码失败回退原串，仍按原串判）', () => {
    expect(sanitizeMRedirect('/m/100%')).toBe('/m/100%')
  })
})

describe('mSitePath / mRoutePathOf', () => {
  it('路由路径补上 /m 前缀；站点路径剥掉前缀（往返一致）', () => {
    expect(mSitePath('/hall?game=3')).toBe('/m/hall?game=3')
    expect(mSitePath('/')).toBe('/m/')
    expect(mRoutePathOf('/m/hall?game=3')).toBe('/hall?game=3')
    expect(mRoutePathOf('/m/')).toBe('/')
    for (const p of ['/hall?game=3', '/mine', '/orders/10000000000000000001']) {
      expect(mRoutePathOf(mSitePath(p))).toBe(p)
    }
  })

  it('M_SITE_BASE 是本文件判据用到的唯一前缀常量', () => {
    expect(M_SITE_BASE).toBe('/m/')
  })
})
