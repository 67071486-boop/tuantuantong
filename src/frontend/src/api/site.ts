import { request } from './http'

/** `GET /api/auth/site` —— 公开的工作室名（登录页在**登录前**要显示，spec §5.5）。- **单飞 + 成功缓存**：登录页与壳可能各要一次，避免重复请求；- **失败回落 `''` 且不缓存失败**：登录页的核心任务是登录，名字拿不到不该拦人，但也不能把一次网络抖动记成永久失败。 */
let inflight: Promise<string> | null = null

export function fetchSiteName(): Promise<string> {
  if (inflight) return inflight
  inflight = request<{ tenant_name?: string }>({
    url: '/api/auth/site',
    method: 'GET',
    skipUnauthorizedHandler: true,
  })
    .then((data) => data.tenant_name ?? '')
    .catch(() => {
      inflight = null
      return ''
    })
  return inflight
}
