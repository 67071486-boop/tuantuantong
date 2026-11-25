// 站内通知接口。契约来源：`src/apps/business/api/notification_views.py` +
// `src/apps/business/urls.py`（**实现即契约**）。端点表见
// `docs/specs/2026-09-20-p3-dispatch-and-performance-design.md` §5。
//
// 与 `api/orders.ts` 同一边界：本文件不 import 任何 UI 库；`request` 在 `http.ts`、
// `Page<T>` 在 `unwrap.ts`。
//
// ## 后端**实际**支持什么（逐条核对过实现，别照对标物的参数来）
//
// | 项 | 事实 | 出处 |
// |---|---|---|
// | 收窄 | queryset 本身就 `filter(user=request.user)`，所以列表**只回自己的**；别人的行表现为「资源不存在」（200 + 业务码），不是 403 | `notification_views.py:48` |
// | 筛选参数 | **一个都不解析**：没有 `isRead`、没有 `msgType`、没有分类。只吃 `EnvelopePagination` 的 `page` / `page_size`（默认 20，上限 200） | `notification_views.py:53-56`；`api/base.py:23-25` |
// | 排序 | `-created_at, -id`（模型 `Meta.ordering`） | `models.py:539` |
// | 标记已读 | **只有单条**：`POST .../{id}/read/`，**幂等**（已读再调仍成功、不动 `read_at`） | `notification_views.py:58-63` |
// | 批量 / 全部已读 | **不存在**（`urls.py:55-64` 只有 list + read 两条；全文无 bulk/all 端点）。对标的 `SetMyMessageRead` 收 `msgId` 逗号列表、空串=全部，本项目**没有**这条形状 | `.tools/recon/suqing1/assets/JS2019/User/mymessage.js:706-710` |
// | 权限 | `IsAuthenticated`（登录即可，没有角色闸门） | `notification_views.py:35` |
//
// ⚠️ 因此**不要**在这里加 `isRead` / `msgType` / `kind` 之类的 query 参数：后端不解析它们，
// 发出去只会让人误以为「筛选生效了」。读/未读与分类的过滤一律在调用方**客户端**做。
import { request } from './http'
import type { Page } from './unwrap'

/** 三类通知的 `kind` **中文值**（后端 `NotificationKind` 存中文，`models.py:495-500`）。 */
export type NotificationKind = '被指派' | '被抢走' | '待审核'

/** 通知的对外形状 = `NotificationSerializer.Meta.fields`（`notification_views.py:28`）。 */
export type Notification = {
  id: number
  kind: NotificationKind | string
  /** 关联订单号；`order` 可空（`SET_NULL`：订单被删后通知仍在）⇒ 也可能是 `null`。 */
  order_no: string | null
  /** ⭐ **来源单号**（D-0301，）：展示回退取「`source_order_no || order_no`」；
   * 实时读 `order` FK（存量 payload 没这个键也能回值）；`order` 为空 ⇒ `null`。 */
  source_order_no: string | null
  /** 展示用载荷：至少 `order_no`，进入某些目标态时另有 `from_status`。**不含金额**。 */
  payload: { order_no?: string; from_status?: string } & Record<string, unknown>
  /** 未读 = `null`。 */
  read_at: string | null
  created_at: string
}

export type NotificationPage = Page<Notification>

const BASE = '/api/business/notifications/'

/** 后端分页上限（`EnvelopePagination.max_page_size = 300`，`api/base.py`）。 */
export const MAX_PAGE_SIZE = 200

/** 列表一页。`data` 形状 = `{items,total,page,page_size}`。 */
export function fetchNotifications(
  params: { page?: number; page_size?: number } = {},
): Promise<NotificationPage> {
  return request<NotificationPage>({ method: 'get', url: BASE, params })
}

/** 取**全部**通知（按 `total` 翻页，一次 200）。为什么要翻页而不是只取第一页：调用方要在客户端把「未读 / 已读」与「分类」两张视图从**同一份**数据里切出来，只取第一页会让「未读表」显示不全、未读计数也不对。循环上界由**第一次响应的 `total`** 定死（`Math.ceil(total / page_size)`），不写成 `while (items.length < total)` 那种「服务端说还有就继续要」的形状——服务端 `total` 与 `items` 不一致时那会变成不停的重试。 */
export async function fetchAllNotifications(): Promise<Notification[]> {
  const first = await fetchNotifications({ page: 1, page_size: MAX_PAGE_SIZE })
  const items = [...first.items]
  const pages = Math.ceil(first.total / MAX_PAGE_SIZE)
  for (let page = 2; page <= pages; page += 1) {
    const next = await fetchNotifications({ page, page_size: MAX_PAGE_SIZE })
    items.push(...next.items)
  }
  return items
}

/** 标记**单条**已读（后端唯一粒度）。幂等：已读的通知再调仍返回成功。
 * 「全部标记为已读」在本项目里只能**逐条**调本函数（后端没有批量端点，见文件头表格）——
 * 那是调用方的组合，不是这里多造一个端点。 */
export function markNotificationRead(id: number): Promise<Notification> {
  return request<Notification>({ method: 'post', url: `${BASE}${id}/read/` })
}
