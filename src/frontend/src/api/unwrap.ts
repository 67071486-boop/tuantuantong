import { ErrorCode } from '@/config/errorCodes'
import { BizError } from './errors'

/** 统一信封：{code,msg,data}。shape 见 docs/ARCHITECTURE.md §8。 */
export type ApiResult<T> = { code: number; msg: string; data: T }

/** 分页载荷，位于 data 内。 */
export type Page<T> = { items: T[]; total: number; page: number; page_size: number }

/** 判断响应体是不是信封。**500 时 Django 返回 HTML，不是信封**（spec §7.2）。 */
export function isEnvelope(body: unknown): body is ApiResult<unknown> {
  return (
    typeof body === 'object' &&
    body !== null &&
    'code' in body &&
    'msg' in body &&
    'data' in body
  )
}

/** 解包成功响应；业务失败抛 BizError。
 * 传输/认证层失败不在此处理——那是 `http.ts` 的 `request<T>()` 在它的 catch 分支里分类的职责
 * （本分支**没有任何 axios 拦截器**，不要按拦截器去找代码）。 */
export function unwrap<T>(res: ApiResult<T>): T {
  if (res.code === ErrorCode.OK) {
    return res.data
  }
  throw new BizError(res.code, res.msg || '操作失败', res.data)
}
