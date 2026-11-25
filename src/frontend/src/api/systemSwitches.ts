// 系统设置开关的两个出口（D-0223，）。
//
// ⚠️ **独立文件**（不塞进 `api/orders.ts` / `api/config.ts`）：与 `api/recognition.ts`
// 同一条分文件理由——并行线也在改那些文件，分文件是刻意的隔离。
//
// 分层与 `api/recognition.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`），
// 本文件不 import 任何 UI 库。
import { request } from './http'

/** 集合路径（后端 `apps/business/urls.py` 的 `path("system-switches/")`）。 */
export const SYSTEM_SWITCHES_URL = '/api/business/system-switches/'

/** 一行的字段（接口 snake_case）。
 * `value` 是 `"Y"` / `"N"`（开关现值）；`default` 是注册表默认（页面不用它渲染，留着自查）；
 * `replica_sk` 是对标的 SK 编号——页面按它到证据件里找对应的那组 radio。 */
export type SystemSwitchRow = {
  key: string
  name: string
  value: 'Y' | 'N'
  default: 'Y' | 'N'
  replica_sk: string
}

/** 列表：注册表默认 ∪ 覆盖行，顺序 = 注册表顺序（今天恰好一条）。 */
export async function fetchSystemSwitches(): Promise<{ items: SystemSwitchRow[] }> {
  return request<{ items: SystemSwitchRow[] }>({
    method: 'get',
    url: SYSTEM_SWITCHES_URL,
  })
}

/** 改一个开关。后端成功回 HTTP 200（项目统一信封），`data` 是保存后的行。
 * `key` 进 URL 前编码（与 recognition 同一口径）；`value` 只收 `"Y"` / `"N"`，
 * 类型上就收窄成这两值——传别的后端回 1000。 */
export async function updateSystemSwitch(
  key: string,
  value: 'Y' | 'N',
): Promise<{ key: string; name: string; value: 'Y' | 'N' }> {
  return request<{ key: string; name: string; value: 'Y' | 'N' }>({
    method: 'patch',
    url: `${SYSTEM_SWITCHES_URL}${encodeURIComponent(key)}/`,
    data: { value },
  })
}
