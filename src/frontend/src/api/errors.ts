/** 类型化错误。`api/` 下**除 `notify.ts` 外**的文件都不 import 任何 UI 库——UI 提示只在 notify.ts。 */

export class BizError extends Error {
  readonly code: number
  readonly data?: unknown

  constructor(code: number, msg: string, data?: unknown) {
    super(msg)
    this.name = 'BizError'
    this.code = code
    this.data = data
  }
}

/** 403：已认证但无权限，或 CSRF 失败（两者的服务端 msg 可辨，见 spec §3.3）。 */
export class HttpError extends Error {
  readonly status: number
  /** true = 这次是 CSRF 失败且我们已重取过 token，调用方提示"请重试"（spec §5.4）。 */
  csrfRefreshed?: boolean
  constructor(status: number, msg: string) {
    super(msg)
    this.name = 'HttpError'
    this.status = status
  }
}

/** 401：未认证/会话失效。**清会话与跳登录不在这里做**——由 `main.ts` 通过
 * `setUnauthorizedHandler()` 注入的处理器负责（见 spec §5.4；`notify.ts` 只管提示）。 */
export class AuthExpiredError extends Error {
  constructor(msg: string) {
    super(msg)
    this.name = 'AuthExpiredError'
  }
}

/** 传输层失败，且**不是业务失败**。两种情况都归这里（**有意放宽了原本只写「没有响应」的语义**）：1. 断网 / 超时 / 请求被取消——**根本没有响应**；2. 响应到了，但**不是统一信封**（如 Django 未预期异常返回的 500 HTML，见 spec §7.2）——无法解析。第 2 种由 `http.request()` 主动抛出。收敛到一个类型是刻意的：`notify.ts`、调用方与测试都只认「这条消息不可信、需要重试或排障」，细分对用户没有区别。 */
export class NetworkError extends Error {
  constructor(msg: string) {
    super(msg)
    this.name = 'NetworkError'
  }
}

/** HTTP 状态码 → 类型化错误。
 * 403 也可能是「该视图没有认证类」而非「已认证但无权限」（见 spec §7.1.1 实测），
 * 因此这里不清 session、不推断登录态。 */
export function mapHttpStatus(status: number, msg: string): HttpError | AuthExpiredError {
  if (status === 401) {
    return new AuthExpiredError(msg || '未认证')
  }
  return new HttpError(status, msg || `请求失败（HTTP ${status}）`)
}
