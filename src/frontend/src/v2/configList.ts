/**
 * v2 配置列表页（游戏维护组）的共享取数零件：自动翻页拉全量 + 软删恢复。
 * 传输/信封复用 `@/api/config` 的既有出口，本模块不碰 UI。
 */
import {
  listConfig,
  updateConfig,
  type ConfigResourceName,
  type ConfigListParams,
  type ConfigRow,
} from '@/api/config'

/** 一次拉满的页大小（后端 `EnvelopePagination.max_page_size` 现为 300，本页保持 200，与 island 三页同值）。 */
const PAGE_SIZE = 200
/** 自动翻页的兜底上限（页数）；到顶仍未取满就停下并如实报 `truncated`。 */
const MAX_PAGES = 25

/** `ConfigListParams` 没有 `deleted` 键，而已删除列表必须带 `deleted: 1`（island `config-replica.ts` L359 口径）——在本模块放宽，⛔ 不改 `api/config.ts`。 */
export type ConfigListParamsDeleted = ConfigListParams & { deleted?: number }

export type ConfigListAll<T> = { items: T[]; total: number; truncated: boolean }

/** 自动翻页取全量：按 `page_size=200` 取页直到 `total` 拿满或 25 页兜底（中途空页即停，防死循环）。
 * `T` 只要求带 `id`：「已删除」层用渲染投影（`DeletedConfigRow`）当 T，不是完整 `ConfigRow`；
 * api 层的约束在取数内部满足、出口按 T 收窄。 */
export async function listAllConfig<T extends { id: number }>(
  resource: ConfigResourceName,
  params: ConfigListParamsDeleted = {},
): Promise<ConfigListAll<T>> {
  const first = await listConfig<ConfigRow>(resource, { ...params, page: 1, page_size: PAGE_SIZE })
  const items = [...(first.items as unknown as T[])]
  let page = 1
  while (items.length < first.total && page < MAX_PAGES) {
    page += 1
    const next = await listConfig<ConfigRow>(resource, { ...params, page, page_size: PAGE_SIZE })
    if (next.items.length === 0) break
    items.push(...(next.items as unknown as T[]))
  }
  return { items, total: first.total, truncated: items.length < first.total }
}

/** 恢复一条软删除的配置 = 普通 `PATCH {deleted_at: null}`（后端无专门端点，island 同口径）。 */
export function restoreConfig<T extends ConfigRow>(
  resource: ConfigResourceName,
  id: number,
): Promise<T> {
  return updateConfig<T>(resource, id, { deleted_at: null })
}

/** 「已删除」列表行共有的三格（三个资源都有）。 */
export type DeletedConfigRow = { id: number; name: string; deleted_at: string | null }

/** ISO 时间串 → `YYYY-MM-DD HH:mm`（「删除时间」列展示用）；取不到原样回。 */
export function formatDeletedAt(raw: string | null | undefined): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(String(raw ?? ''))
  return match ? `${match[1]} ${match[2]}` : String(raw ?? '')
}
