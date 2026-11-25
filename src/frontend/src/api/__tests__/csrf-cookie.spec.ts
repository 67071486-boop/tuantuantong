// @vitest-environment jsdom
// 单独覆盖 environment：本文件必须真的碰 `document.cookie`（`vitest.config.ts` 的全局默认是 node）。
// `http.spec.ts` 则整块 mock 掉 `readCsrfCookie`，所以那边不需要 jsdom。
import { beforeEach, describe, expect, it } from 'vitest'
import { CSRF_COOKIE_NAME, readCsrfCookie } from '../csrf-cookie'

/** 清空 jsdom 的 cookie jar：把现有 cookie 的过期时间设到过去再让它自然消失。 */
function clearCookies(): void {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim()
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT`
  }
}

// 用例之间必须互不污染：jsdom 的 cookie jar 在同一测试文件内是共享的。
beforeEach(() => {
  clearCookies()
})

describe('readCsrfCookie()', () => {
  it('没有 cookie → null', () => {
    expect(document.cookie).toBe('')
    expect(readCsrfCookie()).toBeNull()
  })

  it('单个 csrftoken → 返回它的值', () => {
    document.cookie = 'csrftoken=tok-1'
    expect(readCsrfCookie()).toBe('tok-1')
  })

  it('前后混排多个 cookie → 仍取 csrftoken 的值（不是第一个/最后一个）', () => {
    document.cookie = 'sessionid=abc'
    document.cookie = 'csrftoken=tok-2'
    document.cookie = 'theme=dark'
    expect(readCsrfCookie()).toBe('tok-2')
  })

  it('`notcsrftoken=` 不得误命中（名字要整段匹配，且必须在 `^` 或 `;` 之后）', () => {
    document.cookie = 'notcsrftoken=zzz'
    expect(readCsrfCookie()).toBeNull()
  })

  it('csrftoken 的值为空 → null（而不是空串：空串会被当成"有 token"）', () => {
    document.cookie = 'csrftoken='
    expect(readCsrfCookie()).toBeNull()
  })

  it('百分号编码 → 解码后返回（Django 的 token 不做编码，但 cookie 里可能是编码过的）', () => {
    document.cookie = `csrftoken=${encodeURIComponent('a/b c')}`
    expect(readCsrfCookie()).toBe('a/b c')
  })

  it('读的就是 CSRF_COOKIE_NAME（常量与正则同源，改名字不会静默分叉）', () => {
    document.cookie = `${CSRF_COOKIE_NAME}=tok-3`
    expect(readCsrfCookie()).toBe('tok-3')
  })
})
