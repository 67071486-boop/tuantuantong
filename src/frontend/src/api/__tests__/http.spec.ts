import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  AxiosError,
  type AxiosAdapter,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'
import { AuthExpiredError, BizError, HttpError, NetworkError } from '../errors'
import { http, request, setUnauthorizedHandler } from '../http'
import { readCsrfCookie } from '@/api/csrf-cookie'

/** 取出 promise 抛出的错误。不把断言写在 catch 里：规则 vitest/no-conditional-expect 禁止条件内 expect，而本仓库不允许用规则豁免注释绕过规则。形状与 `src/api/__tests__/unwrap.spec.ts:6-19` 的 `captureThrown` 一致，区别只是这里 await。promise 没抛错时 helper 自己抛，测试同样失败，语义与原「不应该走到这里」一致。 */
async function captureRejected(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise
  } catch (e) {
    return e
  }
  throw new Error('不应该走到这里：预期抛错但没有')
}

/** 假适配器：绕过真实网络，直接给出响应。非 2xx 时按 axios 自己的约定**抛带 `response` 的 `AxiosError`**（真实适配器同此形状，见 `axios/lib/core/settle.js:19-24`：`settle` 在 `!isSuccess` 时用 `response` 构造并 reject），否则 `request()` 的 catch 分支只能看到「没有响应」，测不出状态码分流。 */
function stubAdapter(status: number, data: unknown): AxiosAdapter {
  return async (config: InternalAxiosRequestConfig) => {
    const response: AxiosResponse = {
      data,
      status,
      statusText: '',
      headers: {},
      config,
    }
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

/** 假适配器：模拟「根本没有响应」（断网 / 超时 / 取消）。 */
function stubNoResponse(code?: string): AxiosAdapter {
  return async (config: InternalAxiosRequestConfig) => {
    throw new AxiosError('Network Error', code, config)
  }
}

afterEach(() => {
  // 不要在测试之间留下假适配器：axios 的默认适配器由 `undefined` 触发回退
  //（`axios/lib/core/dispatchRequest.js:52`：adapter 为 undefined 时才回退到默认适配器）。
  http.defaults.adapter = undefined
})

describe('request()', () => {
  it('200 + 信封 code=0 → 返回解包后的 data', async () => {
    http.defaults.adapter = stubAdapter(200, { code: 0, msg: '', data: { id: 7 } })
    await expect(request<{ id: number }>({ url: '/api/x' })).resolves.toEqual({ id: 7 })
  })

  it('200 + 信封 code=1000 → 抛 BizError（业务失败不经 HTTP 状态码）', async () => {
    http.defaults.adapter = stubAdapter(200, {
      code: 1000,
      msg: '该字段是必填项。',
      data: null,
    })
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(BizError)
    expect((e as BizError).code).toBe(1000)
  })

  it('200 但响应体不是信封 → 抛 NetworkError（响应到了却无法解析）', async () => {
    http.defaults.adapter = stubAdapter(200, '<!doctype html><html><body>500</body></html>')
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(NetworkError)
  })

  it('400 + 信封 → HttpError，状态码与后端 msg 都保留', async () => {
    http.defaults.adapter = stubAdapter(400, { code: 1000, msg: '该字段是必填项。', data: null })
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(HttpError)
    expect((e as HttpError).status).toBe(400)
    expect((e as Error).message).toBe('该字段是必填项。')
  })

  it('500 + 非信封（Django 错误页）→ HttpError，msg 走状态码兜底', async () => {
    http.defaults.adapter = stubAdapter(500, '<!doctype html><html><body>Server Error</body></html>')
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(HttpError)
    expect((e as HttpError).status).toBe(500)
    expect((e as Error).message).toBe('请求失败（HTTP 500）')
  })

  it('401 + msg 为 null → 兜底「未认证」，而不是字符串 "null"', async () => {
    http.defaults.adapter = stubAdapter(401, { code: 401, msg: null, data: null })
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(AuthExpiredError)
    expect((e as Error).message).toBe('未认证')
  })

  it('500 + msg 为 null → 兜底带状态码，而不是字符串 "null"', async () => {
    http.defaults.adapter = stubAdapter(500, { code: 500, msg: null, data: null })
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(HttpError)
    expect((e as Error).message).toBe('请求失败（HTTP 500）')
  })

  it('没有任何响应 → NetworkError（不是业务失败）', async () => {
    http.defaults.adapter = stubNoResponse()
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(NetworkError)
    expect((e as Error).message).toBe('网络异常，请检查连接')
    expect(e).not.toBeInstanceOf(HttpError)
  })

  it('超时（ECONNABORTED）→ NetworkError，文案是「请求超时」', async () => {
    http.defaults.adapter = stubNoResponse(AxiosError.ECONNABORTED)
    const e = await captureRejected(request({ url: '/api/x' }))
    expect(e).toBeInstanceOf(NetworkError)
    expect((e as Error).message).toBe('请求超时')
  })
})

// ── 以下为 P5 前端新增（spec §6.1 第 6–12 条）──────────────────────────────
// 注：`vi` / `setUnauthorizedHandler` / `readCsrfCookie` 三个 import 已并入文件头的 import 块
//（brief 的片段把 `vi` 的重复 import 标为「若文件头已 import，去重」；这里同样处理了另外两个，
// 语义不变——ESM import 本就被提升，`vi.mock` 也由 vitest 提升到所有 import 之前）。
vi.mock('@/api/csrf-cookie', () => ({ readCsrfCookie: vi.fn() }))

const cookie = vi.mocked(readCsrfCookie)
const calls: string[] = []
const callsAdapter = (status: number, data: unknown): AxiosAdapter =>
  async (config: InternalAxiosRequestConfig) => {
    calls.push(`${config.method?.toUpperCase()} ${config.url ?? ''}`)
    return stubAdapter(status, data)(config) // 复用本文件既有的 stubAdapter
  }

afterEach(() => {
  setUnauthorizedHandler(null) // 模块级单例必须逐测重置
  // 桩的**返回值**同样会跨测泄漏（`mockClear` 只清调用记录，不清 `mockReturnValue`）：
  // 不复位的话，某个用例的 `mockReturnValue('stale')` 会漏进后面的用例
  //（已用探针复现：不加复位时"下一个用例不设桩也读到 stale"）。
  // 复位后补一个**符合真实默认**的值，免得桩返回 `undefined`（签名是 `string | null`）。
  cookie.mockReset()
  cookie.mockReturnValue(null)
})

describe('CSRF 注入与引导', () => {
  it('cookie 存在 → 带上 X-CSRFToken，且不调 token 端点', async () => {
    calls.length = 0
    cookie.mockReturnValue('tok-1')
    let seen: unknown
    http.defaults.adapter = async (config) => {
      calls.push(`${config.method?.toUpperCase()} ${config.url ?? ''}`)
      seen = config.headers?.['X-CSRFToken']
      return stubAdapter(200, { code: 0, msg: '', data: null })(config)
    }
    await request({ url: '/api/x', method: 'POST' })
    expect(seen).toBe('tok-1')
    expect(calls).toEqual(['POST /api/x'])
  })

  it('cookie 缺失 → 先引导再发原请求（有序）', async () => {
    calls.length = 0
    cookie.mockReturnValue(null)
    http.defaults.adapter = callsAdapter(200, { code: 0, msg: '', data: null })
    await request({ url: '/api/x', method: 'POST' })
    expect(calls).toEqual(['GET /api/auth/csrf', 'POST /api/x'])
  })

  it('CSRF 失败 → 单飞重取一次、不重放原请求、标记 csrfRefreshed', async () => {
    calls.length = 0
    cookie.mockReturnValue('stale')
    const csrfFailure = { code: 403, msg: 'CSRF 校验失败', data: null }
    let first = true
    http.defaults.adapter = async (config) => {
      calls.push(`${config.method?.toUpperCase()} ${config.url ?? ''}`)
      if (config.url === '/api/x') return stubAdapter(403, csrfFailure)(config)
      first = false
      return stubAdapter(200, { code: 0, msg: '', data: null })(config)
    }
    const err = await captureRejected(request({ url: '/api/x', method: 'POST' }))
    expect(first).toBe(false)
    expect(calls).toEqual(['POST /api/x', 'GET /api/auth/csrf']) // **不重放**
    expect(err).toBeInstanceOf(HttpError)
    expect((err as HttpError).csrfRefreshed).toBe(true)
  })

  it('无权限 403 → 不重取 token', async () => {
    calls.length = 0
    cookie.mockReturnValue('tok')
    http.defaults.adapter = callsAdapter(403, {
      code: 403,
      msg: 'You do not have permission to perform this action.',
      data: null,
    })
    const err = await captureRejected(request({ url: '/api/x', method: 'POST' }))
    expect(err).toBeInstanceOf(HttpError)
    expect((err as HttpError).csrfRefreshed).toBeUndefined()
    expect(calls).toEqual(['POST /api/x'])
  })
})

describe('401 分流', () => {
  it('业务请求 401 → 调注入的处理器一次', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    cookie.mockReturnValue('tok')
    http.defaults.adapter = stubAdapter(401, { code: 401, msg: '未认证', data: null })
    await captureRejected(request({ url: '/api/x' }))
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('skipUnauthorizedHandler: true 的 401 → **不调**处理器（登录/引导 me/CSRF 重取）', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    cookie.mockReturnValue('tok')
    http.defaults.adapter = stubAdapter(401, { code: 401, msg: '用户名或密码错误', data: null })
    await captureRejected(request({ url: '/api/auth/login', method: 'POST', skipUnauthorizedHandler: true }))
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('默认（不传标志）就是 fail-closed：401 会调处理器', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    cookie.mockReturnValue('tok')
    http.defaults.adapter = stubAdapter(401, { code: 401, msg: '未认证', data: null })
    await captureRejected(request({ url: '/api/whatever' }))
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })
})

// ── 以下为 spec §6.1 第 8②③④ 的用例（brief 的片段只给了 ①⑤）──────────────
// 判据：只断「总次数」会让「每次写请求都先预取 token」「单飞没有 finally 复位」这类坏实现假绿，
// 所以 ③ 断**有序/分类调用表**，② 断**注入的两个不同值**。
describe('CSRF token 生命周期（不缓存 / 单飞 / 一次即止）', () => {
  /** 写请求 403（CSRF 文案）、token 端点 200 的适配器；逐次把 (method,url) 记进 calls。 */
  const csrfFailAdapter: AxiosAdapter = async (config: InternalAxiosRequestConfig) => {
    calls.push(`${config.method?.toUpperCase()} ${config.url ?? ''}`)
    if (config.url === '/api/x') {
      return stubAdapter(403, { code: 403, msg: 'CSRF 校验失败', data: null })(config)
    }
    return stubAdapter(200, { code: 0, msg: '', data: null })(config)
  }

  it('第 8②：cookie 换值 → 第二次写请求带新值（每次都现读，不缓存 token）', async () => {
    calls.length = 0
    const seen: unknown[] = []
    cookie.mockReturnValueOnce('tok-old').mockReturnValue('tok-new')
    http.defaults.adapter = async (config) => {
      calls.push(`${config.method?.toUpperCase()} ${config.url ?? ''}`)
      seen.push(config.headers?.['X-CSRFToken'])
      return stubAdapter(200, { code: 0, msg: '', data: null })(config)
    }
    await request({ url: '/api/x', method: 'POST' })
    await request({ url: '/api/x', method: 'POST' })
    expect(seen).toEqual(['tok-old', 'tok-new'])
    expect(calls).toEqual(['POST /api/x', 'POST /api/x'])
  })

  it('第 8③：并发 3 个写请求同时 CSRF 失败 → token 端点**恰好 1 次**（单飞）', async () => {
    calls.length = 0
    cookie.mockReturnValue('stale')
    http.defaults.adapter = csrfFailAdapter
    const errs = await Promise.all([
      captureRejected(request({ url: '/api/x', method: 'POST' })),
      captureRejected(request({ url: '/api/x', method: 'POST' })),
      captureRejected(request({ url: '/api/x', method: 'POST' })),
    ])
    // 三次原请求 + 一次 token 端点；只断总次数会被「每个请求各重取一次」骗过，
    // 所以这里同时断言「GET /api/auth/csrf 恰 1 次」与「calls 总长 4」（没有第三次重取）。
    expect(calls.filter((c) => c === 'POST /api/x')).toHaveLength(3)
    expect(calls.filter((c) => c === 'GET /api/auth/csrf')).toHaveLength(1)
    expect(calls).toHaveLength(4)
    for (const e of errs) {
      expect(e).toBeInstanceOf(HttpError)
      expect((e as HttpError).csrfRefreshed).toBe(true)
    }
  })

  it('第 8③：重取完成后单飞句柄复位（finally）——顺序第二次失败会**重新**取 token', async () => {
    calls.length = 0
    cookie.mockReturnValue('stale')
    http.defaults.adapter = csrfFailAdapter
    await captureRejected(request({ url: '/api/x', method: 'POST' }))
    await captureRejected(request({ url: '/api/x', method: 'POST' }))
    // 若 `csrfInflight` 没有在 finally 里复位，第二次会复用已完成的 promise → 少一次 GET。
    expect(calls).toEqual([
      'POST /api/x',
      'GET /api/auth/csrf',
      'POST /api/x',
      'GET /api/auth/csrf',
    ])
  })

  it('第 8④：重取请求自身 401 → 一次即止 + 明确报错，且**不调** 401 处理器（重取请求带 skipUnauthorizedHandler）', async () => {
    calls.length = 0
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    cookie.mockReturnValue('stale')
    http.defaults.adapter = async (config) => {
      calls.push(`${config.method?.toUpperCase()} ${config.url ?? ''}`)
      if (config.url === '/api/x') {
        return stubAdapter(403, { code: 403, msg: 'CSRF 校验失败', data: null })(config)
      }
      return stubAdapter(401, { code: 401, msg: '未认证', data: null })(config)
    }
    const err = await captureRejected(request({ url: '/api/x', method: 'POST' }))
    expect(err).toBeInstanceOf(NetworkError)
    expect((err as Error).message).toBe('无法获取 CSRF 令牌')
    expect(calls).toEqual(['POST /api/x', 'GET /api/auth/csrf']) // 没有第三次
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('第 8④：引导重取 200 但仍读不到 cookie → 原请求 403 后**不再重取第二次**，一次即止 + 明确报错', async () => {
    calls.length = 0
    cookie.mockReturnValue(null) // 引导后依然读不到
    http.defaults.adapter = csrfFailAdapter
    const err = await captureRejected(request({ url: '/api/x', method: 'POST' }))
    expect(err).toBeInstanceOf(NetworkError)
    expect((err as Error).message).toBe('无法获取 CSRF 令牌')
    // 「本次请求已重取过」的标志挂在局部变量上 → 403 不再触发第二次重取。
    expect(calls).toEqual(['GET /api/auth/csrf', 'POST /api/x'])
  })

  it('第 8④：重取成功（200）但仍读不到 cookie → 一次即止 + 明确报错', async () => {
    calls.length = 0
    // 首读**真**（因此不走引导、直接发原请求）→ 重取之后读不到：只有 `http.ts` 里
    // 「重取后复查 cookie」那一条分支能拦住它（否则会带空 token 再发一次原请求）。
    cookie.mockReturnValueOnce('stale').mockReturnValue(null)
    http.defaults.adapter = csrfFailAdapter // /api/x → 403 CSRF；/api/auth/csrf → 200
    const err = await captureRejected(request({ url: '/api/x', method: 'POST' }))
    expect(err).toBeInstanceOf(NetworkError)
    expect((err as Error).message).toBe('无法获取 CSRF 令牌')
    expect((err as HttpError).csrfRefreshed).toBeUndefined() // 没刷新成功，不许谎报
    expect(calls).toEqual(['POST /api/x', 'GET /api/auth/csrf']) // 不重放、无第三次
  })

  it('不修改调用方传入的 config（CSRF 头只补在本地副本上）', async () => {
    calls.length = 0
    cookie.mockReturnValue('tok')
    http.defaults.adapter = callsAdapter(200, { code: 0, msg: '', data: null })
    const config = { url: '/api/x', method: 'POST' as const }
    await request(config)
    expect(calls).toEqual(['POST /api/x'])
    expect(config).toEqual({ url: '/api/x', method: 'POST' }) // 没有多出 headers 键
  })
})
