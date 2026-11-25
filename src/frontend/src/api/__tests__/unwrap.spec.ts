import { describe, expect, it } from 'vitest'
import { ErrorCode } from '@/config/errorCodes'
import { BizError } from '../errors'
import { isEnvelope, unwrap, type ApiResult } from '../unwrap'

/** 取出 fn 抛出的错误。不把断言写在 catch 里：规则 vitest/no-conditional-expect 禁止条件内 expect，而本仓库不允许用规则豁免注释绕过规则。fn 没抛错时 helper 自己抛，测试同样失败，语义与原「不应该走到这里」一致。 */
function captureThrown(fn: () => unknown): unknown {
  try {
    fn()
  } catch (e) {
    return e
  }
  throw new Error('不应该走到这里：预期抛错但没有')
}

describe('unwrap', () => {
  it('code=0 时返回 data', () => {
    const res: ApiResult<{ id: number }> = { code: 0, msg: '', data: { id: 7 } }
    expect(unwrap(res)).toEqual({ id: 7 })
  })

  it('code=1000（校验失败）抛 BizError 且带 code 与 msg', () => {
    const res: ApiResult<null> = { code: ErrorCode.VALIDATION, msg: '该字段是必填项。', data: null }
    const e = captureThrown(() => unwrap(res))
    expect(e).toBeInstanceOf(BizError)
    expect((e as BizError).code).toBe(1000)
    expect((e as BizError).message).toBe('该字段是必填项。')
  })

  it('code=1002（业务失败）抛 BizError', () => {
    const res: ApiResult<null> = { code: ErrorCode.BUSINESS, msg: '资源不存在', data: null }
    expect(() => unwrap(res)).toThrow(BizError)
  })

  it('保留后续动作失败时已建单的系统单号', () => {
    const data = { order_no: '20261118000000000001', reason: '大厅暂不可用' }
    const e = captureThrown(() => unwrap({ code: 1023, msg: '单已建好，发大厅失败', data }))
    expect(e).toMatchObject({ code: 1023, data })
  })

  it('msg 为空时给出兜底文案，不抛空消息', () => {
    const res: ApiResult<null> = { code: ErrorCode.BUSINESS, msg: '', data: null }
    expect(() => unwrap(res)).toThrow(/失败|错误/)
  })
})

describe('isEnvelope', () => {
  it('识别真实信封', () => {
    expect(isEnvelope({ code: 0, msg: '', data: null })).toBe(true)
  })

  it('HTML 错误页（500 的情况）不是信封', () => {
    expect(isEnvelope('<!doctype html><html><body>Server Error</body></html>')).toBe(false)
  })

  it('null 与普通对象不是信封', () => {
    expect(isEnvelope(null)).toBe(false)
    expect(isEnvelope({ status: 'ok' })).toBe(false)
  })
})
