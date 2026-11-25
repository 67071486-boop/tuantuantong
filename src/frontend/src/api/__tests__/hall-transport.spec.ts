import { afterEach, describe, expect, it, vi } from 'vitest'
import { AxiosError, type AxiosAdapter, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { BizError, HttpError } from '../errors'
import { http } from '../http'
import { claimOrder, fetchHall, throwToHall, withdrawThrow } from '../hall'

/** ⚠️ **对 brief 的最小偏离（必须回报控制方）**：brief 原文没有这一段，而它**跑不过**——本仓库 `vitest.config.ts` 的默认环境是 `node`（无 `document`），而三个写动作（抢单/抛单/撤回）都会走 `http.ts` 的 CSRF 注入分支去读 `document.cookie`，于是在**到达假适配器之前**就抛`ReferenceError: document is not defined`（实测：5 条里 4 条红）。`http.spec.ts` 对同一问题的既有做法就是整块 mock 这个模块（`csrf-cookie.ts` 的 docstring也写明「抽成独立模块是为了让 http.spec.ts 整块 mock 它，从而留在 environment: 'node'」）。这里沿用同一手法：给一个**非空** token，让写请求直接走到适配器、**不触发 CSRF 引导**，于是最后一条断言里的 4 次调用就是这 4 个业务动作本身。 */
vi.mock('@/api/csrf-cookie', () => ({ readCsrfCookie: () => 'tok-hall-transport' }))

/** **真** `request()`（不 mock `@/api/http`）：只把 axios 的适配器换成假响应。
 * 这一组是本任务**判别力**的落点：它打的是 `api/http.ts` 的**统一分支**，而不是我们的封装。
 * 适配器的形状与理由逐字沿用 `http.spec.ts:31-53`。 */
function stubAdapter(status: number, data: unknown): AxiosAdapter {
  return async (config: InternalAxiosRequestConfig) => {
    const response: AxiosResponse = { data, status, statusText: '', headers: {}, config }
    if (status >= 200 && status < 300) return response
    throw new AxiosError(
      `Request failed with status code ${status}`,
      status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST,
      config,
      null,
      response,
    )
  }
}

/** 取出 promise 抛出的错误（不把断言写进 catch：`vitest/no-conditional-expect`）。 */
async function captureRejected(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise
  } catch (e) {
    return e
  }
  throw new Error('不应该走到这里：预期抛错但没有')
}

afterEach(() => {
  http.defaults.adapter = undefined
})

const NO = '12345678901234567890'

describe('hall 出口走 api/http 的统一分支', () => {
  it('200 + code 0 → 解包出 data', async () => {
    http.defaults.adapter = stubAdapter(200, { code: 0, msg: '', data: { items: [], total: 0 } })
    await expect(fetchHall()).resolves.toEqual({ items: [], total: 0 })
  })

  it('code 1013（抢单限制）当成功 = 红：必须抛 BizError 且 code/msg 都来自后端', async () => {
    http.defaults.adapter = stubAdapter(200, {
      code: 1013,
      msg: '接单价必须在 0~99999 之间',
      data: null,
    })
    const err = await captureRejected(claimOrder(NO))
    expect(err).toBeInstanceOf(BizError)
    expect((err as BizError).code).toBe(1013)
    expect((err as BizError).message).toBe('接单价必须在 0~99999 之间')
  })

  it('code 1012（已被别人接走）同样抛 BizError，msg 原样', async () => {
    http.defaults.adapter = stubAdapter(200, {
      code: 1012,
      msg: '该订单已被别人接走',
      data: null,
    })
    const err = await captureRejected(claimOrder(NO))
    expect((err as BizError).message).toBe('该订单已被别人接走')
  })

  it('HTTP 403 → HttpError(403, 后端 msg)：抢单/抛单/撤回都走**同一条**分支', async () => {
    http.defaults.adapter = stubAdapter(403, {
      code: 403,
      msg: '只有代练员可以抢单',
      data: null,
    })
    const claimErr = await captureRejected(claimOrder(NO))
    expect(claimErr).toBeInstanceOf(HttpError)
    expect((claimErr as HttpError).status).toBe(403)
    expect((claimErr as HttpError).message).toBe('只有代练员可以抢单')

    http.defaults.adapter = stubAdapter(403, {
      code: 403,
      msg: '你没有改接单价的权限',
      data: null,
    })
    const throwErr = await captureRejected(throwToHall(NO, '1.00'))
    expect((throwErr as HttpError).status).toBe(403)
    expect((throwErr as HttpError).message).toBe('你没有改接单价的权限')

    http.defaults.adapter = stubAdapter(403, { code: 403, msg: '无权限', data: null })
    const withdrawErr = await captureRejected(withdrawThrow(NO))
    expect((withdrawErr as HttpError).status).toBe(403)
    expect((withdrawErr as HttpError).message).toBe('无权限')
  })

  it('请求真的打到了那两个路径（URL 写错要在这里红）', async () => {
    const calls: string[] = []
    http.defaults.adapter = (async (config: InternalAxiosRequestConfig) => {
      calls.push(`${String(config.method).toUpperCase()} ${config.url}`)
      return { data: { code: 0, msg: '', data: null }, status: 200, statusText: '', headers: {}, config }
    }) as AxiosAdapter

    await claimOrder(NO)
    await throwToHall(NO, '1.00')
    await withdrawThrow(NO)
    await fetchHall({ page: 1, page_size: 20 })

    expect(calls).toEqual([
      `POST /api/business/hall/${NO}/claim/`,
      `POST /api/business/orders/${NO}/throw-to-hall/`,
      `POST /api/business/orders/${NO}/withdraw/`,
      'GET /api/business/hall/',
    ])
  })
})
