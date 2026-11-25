/** 「找回删除的订单」v2 的**本地薄封装**（`src/v2/**` 内，⛔ 不改 `api/orders.ts`）。
 *
 * 为什么要有这个文件：回收站列表端点在共享出口 `api/orders.ts` 里**没有现成函数**——
 * v1 island 页走 layui 的 `url` 通道自己发取数请求（`order-orderrecycle.ts` 的
 * `RECYCLE_API_URL`；⛔ 不 import island 页模块，那会把 layui 装载链拉进 v2 模块图）⇒
 * v2 在此本地声明 URL，复用同一个信封出口 `request`；类型复用 `@/api/orders` 的
 * `RecycleBinRow` / `RecycleBinSearchParams`，不复制第二份契约。
 */
import { request } from '@/api/http'
import type { RecycleBinRow, RecycleBinSearchParams } from '@/api/orders'
import type { Page } from '@/api/unwrap'

/** 回收站列表端点（后端 `RecycleBinSearchSerializer` 对应的 viewset 动作 path）。 */
const RECYCLE_BIN_URL = '/api/business/orders/recycle-bin/'

/**
 * 查询参数的空值丢弃（v1 `toRecycleSearch` 同口径重写）：null / 空串 / 纯空白一律丢，
 * 「全部游戏」「全部」这些首项才不会变成筛选条件；**数字 0 照发**（E-0021：判据是
 * 「有没有给」，不是真值）。`page` / `page_size` 由调用方必传，原样透传。
 */
export function toRecycleBinParams(params: RecycleBinSearchParams): RecycleBinSearchParams {
  const out: RecycleBinSearchParams = {}
  if (params.game !== null && params.game !== undefined) out.game = params.game
  if (params.status !== undefined && params.status !== '') out.status = params.status
  if (params.deleted_from !== undefined && params.deleted_from !== '') {
    out.deleted_from = params.deleted_from
  }
  if (params.deleted_to !== undefined && params.deleted_to !== '') {
    out.deleted_to = params.deleted_to
  }
  if (params.order_source !== null && params.order_source !== undefined) {
    out.order_source = params.order_source
  }
  const keyword = params.keyword?.trim() ?? ''
  if (keyword !== '') out.keyword = keyword
  const account = params.game_account?.trim() ?? ''
  if (account !== '') out.game_account = account
  if (params.page !== undefined) out.page = params.page
  if (params.page_size !== undefined) out.page_size = params.page_size
  return out
}

/** 取回收站列表一页（统一信封 / 错误映射都在 `api/http` 一处）。 */
export async function fetchRecycleBinPage(
  params: RecycleBinSearchParams,
): Promise<Page<RecycleBinRow>> {
  return request<Page<RecycleBinRow>>({
    method: 'get',
    url: RECYCLE_BIN_URL,
    params: toRecycleBinParams(params),
  })
}
