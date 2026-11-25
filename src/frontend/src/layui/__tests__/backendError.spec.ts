import { describe, expect, it } from 'vitest'

import { backendErrorMessage } from '../backendError'

/** 兜底文案的字面值（按 E-0024：断言钉值，不钉导出的常量）。 */
const FALLBACK = '请求失败，请稍后重试'

describe('backendErrorMessage：把后端那句 msg 从两种形状里取出来', () => {
  it('统一信封 `{code,msg}`：直接给 `msg`', () => {
    expect(backendErrorMessage({ code: 403, msg: '只有代练员可以抢单', data: null })).toBe(
      '只有代练员可以抢单',
    )
  })

  it('真实浏览器里的 **jqXHR**：msg 在 `responseJSON.msg`（只读 `res.msg` 会 undefined）', () => {
    expect(
      backendErrorMessage({
        readyState: 4,
        responseText: '{"code":403,"msg":"只有客服或客服主管可以查看绩效列表","data":null}',
        responseJSON: { code: 403, msg: '只有客服或客服主管可以查看绩效列表', data: null },
        status: 403,
        statusText: 'Forbidden',
      }),
    ).toBe('只有客服或客服主管可以查看绩效列表')
  })

  it('只有 `responseText`（没有 `responseJSON`）：退回解析 JSON 取 `msg`', () => {
    expect(backendErrorMessage({ status: 403, responseText: '{"code":403,"msg":"没有权限"}' })).toBe(
      '没有权限',
    )
  })

  it('认不出形状（Error / HTML 错误页 / 空对象 / 非对象）⇒ 中性中文，**不出现英文 `error`**', () => {
    const cases: unknown[] = [
      new Error('error'),
      '<!DOCTYPE html><html>…</html>',
      'error',
      {},
      { status: 500 },
      { responseText: '<!DOCTYPE html><html>…</html>' },
      { msg: '' },
      null,
      undefined,
      0,
    ]
    for (const res of cases) {
      const text = backendErrorMessage(res)
      expect(text).toBe(FALLBACK)
      expect(text).not.toContain('error')
    }
  })
})
