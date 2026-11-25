// @vitest-environment jsdom
/** `mobileRedirect.ts` 的判据测试（2026-10-12 夜间线；UA 一律合成值）。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  MOBILE_REDIRECT_SUPPRESS_KEY,
  clearMobileRedirectSuppressed,
  readMobileRedirectSuppressed,
  redirectToMobileIfMobile,
  shouldRedirectToMobile,
  writeMobileRedirectSuppressed,
  type MobileRedirectStorage,
} from '../mobileRedirect'

const DESKTOP_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
const ANDROID_UA =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36'

beforeEach(() => {
  // 抑制标记住在真 sessionStorage 里（jsdom 有）⇒ 每个用例自己收口，免得上一条污染下一条。
  window.sessionStorage.clear()
})

describe('shouldRedirectToMobile', () => {
  it('opens registered visitor chat within the same app on phones', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/visitor-chat')).toBe(false)
    expect(shouldRedirectToMobile(ANDROID_UA, '/visitor-chat/', false, '?wide=1')).toBe(false)
    expect(shouldRedirectToMobile(ANDROID_UA, '/visitor-chat-other')).toBe(true)
  })
  it('keeps staff approval push links and their sign-in return path in the desktop app', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/v2/approvals')).toBe(false)
    expect(shouldRedirectToMobile(IPHONE_UA, '/login', false, '?redirect=%2Fv2%2Fapprovals')).toBe(false)
    expect(shouldRedirectToMobile(IPHONE_UA, '/login', false, '?redirect=%2Fv2%2Fother')).toBe(true)
  })

  it('手机 UA（iPhone / Android）⇒ 跳', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/')).toBe(true)
    expect(shouldRedirectToMobile(ANDROID_UA, '/order/list')).toBe(true)
  })

  it('桌面 UA ⇒ 不跳；空值/非串 ⇒ 不跳（不抛）', () => {
    expect(shouldRedirectToMobile(DESKTOP_UA, '/')).toBe(false)
    expect(shouldRedirectToMobile('', '/')).toBe(false)
    expect(shouldRedirectToMobile(undefined as unknown as string, '/')).toBe(false)
  })

  it('已经在 /m/ 下 ⇒ 不跳（防御性；生产上桌面壳只会在非 /m/ 路径被加载）', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/m/')).toBe(false)
    expect(shouldRedirectToMobile(IPHONE_UA, '/m/hall')).toBe(false)
  })

  it('路径里**含** m 但不是 /m/ 前缀 ⇒ 照跳（前缀判据，不是 contains）', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/order/m-list')).toBe(true)
  })

  it('有抑制标记 ⇒ 一律不跳（手机 UA + 桌面路径也不跳；D-A3 的死循环钉子）', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/', true)).toBe(false)
    expect(shouldRedirectToMobile(ANDROID_UA, '/order/list', true)).toBe(false)
    // 对照：同一条 UA + 路径、只把标记去掉 ⇒ 必须跳（证明上面那条不是恒 false）。
    expect(shouldRedirectToMobile(IPHONE_UA, '/', false)).toBe(true)
  })

  it('第三参缺省 = 没有标记（既有两参调用的语义逐字不变）', () => {
    expect(shouldRedirectToMobile(IPHONE_UA, '/')).toBe(true)
    expect(shouldRedirectToMobile(DESKTOP_UA, '/')).toBe(false)
  })
})

describe('抑制标记的读写（storage 可注入）', () => {
  /** 替身 storage：真值表用；`fail` 打开时三个动作都抛（模拟隐私模式 / 配额）。 */
  function fakeStorage(fail = false): MobileRedirectStorage & { readonly size: number } {
    const map = new Map<string, string>()
    const boom = (): never => {
      throw new Error('storage 不可用')
    }
    return {
      getItem: (k) => (fail ? boom() : (map.get(k) ?? null)),
      setItem: (k, v) => {
        if (fail) boom()
        map.set(k, v)
      },
      removeItem: (k) => {
        if (fail) boom()
        map.delete(k)
      },
      get size() {
        return map.size
      },
    }
  }

  it('空 storage ⇒ 读 false；写入 ⇒ true；清掉 ⇒ false（写的是约定键）', () => {
    const s = fakeStorage()
    expect(readMobileRedirectSuppressed(s)).toBe(false)
    writeMobileRedirectSuppressed(s)
    expect(readMobileRedirectSuppressed(s)).toBe(true)
    expect(s.getItem(MOBILE_REDIRECT_SUPPRESS_KEY)).not.toBeNull()
    clearMobileRedirectSuppressed(s)
    expect(readMobileRedirectSuppressed(s)).toBe(false)
    expect(s.size).toBe(0)
  })

  it('storage 缺席（null，非浏览器环境）⇒ 读 false、写/清不抛', () => {
    expect(readMobileRedirectSuppressed(null)).toBe(false)
    expect(() => writeMobileRedirectSuppressed(null)).not.toThrow()
    expect(() => clearMobileRedirectSuppressed(null)).not.toThrow()
  })

  it('storage 抛异常（隐私模式 / 配额）⇒ 读退化成 false、写/清不抛', () => {
    const s = fakeStorage(true)
    expect(readMobileRedirectSuppressed(s)).toBe(false)
    expect(() => writeMobileRedirectSuppressed(s)).not.toThrow()
    expect(() => clearMobileRedirectSuppressed(s)).not.toThrow()
  })

  it('缺省实现走真 sessionStorage：写 → 读 → 清 一趟往返', () => {
    expect(readMobileRedirectSuppressed()).toBe(false)
    writeMobileRedirectSuppressed()
    expect(window.sessionStorage.getItem(MOBILE_REDIRECT_SUPPRESS_KEY)).toBe('1')
    expect(readMobileRedirectSuppressed()).toBe(true)
    clearMobileRedirectSuppressed()
    expect(readMobileRedirectSuppressed()).toBe(false)
  })
})

describe('redirectToMobileIfMobile', () => {
  /** jsdom 的 location 是只读的 ⇒ 换成可控替身跑一段，跑完必须还原（否则污染后面的用例）。 */
  function withStubbedLocation(fn: (replace: ReturnType<typeof vi.fn>) => void): void {
    const replace = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...original, replace },
    })
    try {
      fn(replace)
    } finally {
      Object.defineProperty(window, 'location', {
        configurable: true,
        writable: true,
        value: original,
      })
    }
  }

  it('手机 UA ⇒ `location.replace("/m/")`；桌面 UA ⇒ 一个跳转都不发', () => {
    withStubbedLocation((replace) => {
      redirectToMobileIfMobile(IPHONE_UA, '/')
      expect(replace).toHaveBeenCalledWith('/m/')
      replace.mockClear()
      redirectToMobileIfMobile(DESKTOP_UA, '/')
      expect(replace).not.toHaveBeenCalled()
    })
  })

  it('显式带抑制标记 ⇒ 手机 UA 也一个跳转都不发', () => {
    withStubbedLocation((replace) => {
      redirectToMobileIfMobile(IPHONE_UA, '/', true)
      expect(replace).not.toHaveBeenCalled()
      // 对照：同一条 UA + 路径、标记为 false ⇒ 必须发（证明上面那条不是恒不发）。
      redirectToMobileIfMobile(IPHONE_UA, '/', false)
      expect(replace).toHaveBeenCalledWith('/m/')
    })
  })

  it('⭐ 死循环反向钉子：被弹回后（标记已落 sessionStorage）桌面入口不再跳 /m/', () => {
    // 「被弹回」= 守卫写的那个标记；这里只落标记、不看守卫（守卫那一半在 guards.spec.ts 里跑真流程）。
    writeMobileRedirectSuppressed()
    withStubbedLocation((replace) => {
      // 第三参缺省 ⇒ 走真 sessionStorage 的读取路径（就是 main.ts 的调用形态）。
      redirectToMobileIfMobile(IPHONE_UA, '/')
      expect(replace).not.toHaveBeenCalled()
    })
  })
})
