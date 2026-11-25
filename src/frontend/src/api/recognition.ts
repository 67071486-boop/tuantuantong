// 录单识别设置的两个出口（spec §7.1 / §5）。
//
// ⚠️ **独立文件**（不塞进 `api/orders.ts` / `api/config.ts`）：那两份是并行线也在改的文件
// （同 `api/orderSources.ts` 的分文件理由），分文件是刻意的隔离。
//
// 分层与 `api/orderSources.ts` 同形：传输、认证与信封解包全在 `request()`（`./http.ts`）里，
// 本文件不 import 任何 UI 库。
import { request } from './http'

/** 集合路径（后端 `apps/business/urls.py` 的 `path("recognition-settings/")`）。 */
export const RECOGNITION_SETTINGS_URL = '/api/business/recognition-settings/'

/** 一行的字段（接口 snake_case；`keywords` 是逗号/空格分隔的**原文串**）。
 * `keywords` 允许空串：清空 = 这一项不再产出任何识别规则（合法且有意义的覆盖，不是「没填」）。
 * 字段名照 §5.1 的契约——`GET` 回 `data = {items: […]}`，`PATCH` 回保存后的整行。 */
export type RecognitionItemRow = {
  item_code: string
  name: string
  keywords: string
}

/** 列表：后端返回 **14 行固定顺序**（顺序即页面顺序），页面不再排一遍。 */
export async function fetchRecognitionSettings(): Promise<{ items: RecognitionItemRow[] }> {
  return request<{ items: RecognitionItemRow[] }>({
    method: 'get',
    url: RECOGNITION_SETTINGS_URL,
  })
}

/** 改一行的关键词。后端成功回 HTTP 200（项目统一信封），`data` 是保存后的整行。
 * `item_code` 进 URL 前编码：它直接来自后端行数据，不编码会在含 `/` 等字符时打到别的路径。
 * 空的 `keywords` 是**合法载荷**（清空该项），原样发出去——按假值丢弃会让「清空」变成静默失败。 */
export async function updateRecognitionKeywords(
  itemCode: string,
  keywords: string,
): Promise<RecognitionItemRow> {
  return request<RecognitionItemRow>({
    method: 'patch',
    url: `${RECOGNITION_SETTINGS_URL}${encodeURIComponent(itemCode)}/`,
    data: { keywords },
  })
}
