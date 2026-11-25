import { describe, expect, it } from 'vitest'
import { AuthExpiredError, HttpError, NetworkError, mapHttpStatus } from '../errors'

describe('mapHttpStatus', () => {
  it('401 → AuthExpiredError（要清 session）', () => {
    expect(mapHttpStatus(401, '')).toBeInstanceOf(AuthExpiredError)
  })

  it('401 且 msg 为空 → 兜底文案是「未认证」', () => {
    expect(mapHttpStatus(401, '').message).toBe('未认证')
  })

  it('403 → HttpError，且 status=403（不清 session）', () => {
    const e = mapHttpStatus(403, '无权限')
    expect(e).toBeInstanceOf(HttpError)
    expect((e as HttpError).status).toBe(403)
    expect(e.message).toBe('无权限')
  })

  it('400 / 405 / 429 / 500 → HttpError，保留状态码', () => {
    for (const s of [400, 405, 429, 500]) {
      const e = mapHttpStatus(s, '')
      expect(e).toBeInstanceOf(HttpError)
      expect((e as HttpError).status).toBe(s)
    }
  })

  it('msg 为空时兜底文案带状态码', () => {
    expect(mapHttpStatus(500, '').message).toContain('500')
  })
})

describe('NetworkError', () => {
  it('是 Error 子类，name 与 message 原样保留', () => {
    const e = new NetworkError('x')
    expect(e).toBeInstanceOf(Error)
    expect(e.name).toBe('NetworkError')
    expect(e.message).toBe('x')
  })
})
