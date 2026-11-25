import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  MAX_PAGE_SIZE,
  fetchAllNotifications,
  fetchNotifications,
  markNotificationRead,
  type Notification,
} from '../notifications'

/** 站内通知接口层（`api/notifications.ts`）的**请求形状**测试。钉的是真实出口：**方法 + URL + 载荷**。这两条端点的后端实现逐条核过（`apps/business/api/notification_views.py`、`apps/business/urls.py:55-64`）：- list 只解析分页，**没有任何筛选参数**；- read **只有单条**，路径参数名是 `notification_id`（`<int:notification_id>/read/`）。 */
const mockedRequest = vi.mocked(request)

const notification = (id: number, kind: string, read: string | null = null): Notification => ({
  id,
  kind,
  order_no: `12345678901234567${String(id).padStart(3, '0')}`,
  source_order_no: null,
  payload: { order_no: `12345678901234567${String(id).padStart(3, '0')}`, from_status: '进行中' },
  read_at: read,
  created_at: '2026-09-21T08:00:00+08:00',
})

type Call = { method?: string; url?: string; params?: unknown; data?: unknown }
const calls: Call[] = []

beforeEach(() => {
  calls.length = 0
  mockedRequest.mockReset()
  mockedRequest.mockImplementation(async (config: Call) => {
    calls.push(config)
    return { items: [], total: 0, page: 1, page_size: MAX_PAGE_SIZE }
  })
})

describe('列表：GET /api/business/notifications/', () => {
  it('方法 / URL / 只带分页参数（后端不解析 isRead、msgType 之类的筛选）', async () => {
    await fetchNotifications({ page: 1, page_size: 200 })
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(calls[0]).toEqual({
      method: 'get',
      url: '/api/business/notifications/',
      params: { page: 1, page_size: 200 },
    })
  })

  it('不传参数时也**不**自己发明筛选参数', async () => {
    await fetchNotifications()
    expect(calls[0]!.params).toEqual({})
  })
})

describe('fetchAllNotifications：按 total 翻页取全量', () => {
  it('total 落在第一页里 ⇒ 只发一次请求', async () => {
    mockedRequest.mockImplementation(async (config: Call) => {
      calls.push(config)
      return { items: [notification(1, '被指派')], total: 1, page: 1, page_size: MAX_PAGE_SIZE }
    })
    const all = await fetchAllNotifications()
    expect(all.map((n) => n.id)).toEqual([1])
    expect(calls.map((c) => c.params)).toEqual([{ page: 1, page_size: MAX_PAGE_SIZE }])
  })

  it('total > 一页 ⇒ 按 ceil(total/200) 继续翻，且把 items 拼起来', async () => {
    mockedRequest.mockImplementation(async (config: Call & { params?: { page?: number } }) => {
      calls.push(config)
      const page = config.params?.page ?? 1
      return {
        items: [notification(page, '待审核')],
        total: 250,
        page,
        page_size: MAX_PAGE_SIZE,
      }
    })
    const all = await fetchAllNotifications()
    expect(all.map((n) => n.id)).toEqual([1, 2])
    expect(calls.map((c) => c.params)).toEqual([
      { page: 1, page_size: MAX_PAGE_SIZE },
      { page: 2, page_size: MAX_PAGE_SIZE },
    ])
  })

  it('total 为 0 ⇒ 只发一次请求，不回空转', async () => {
    const all = await fetchAllNotifications()
    expect(all).toEqual([])
    expect(calls).toHaveLength(1)
  })
})

describe('标记已读：POST /api/business/notifications/{id}/read/', () => {
  it('后端只有**单条**端点：URL 带 id、方法是 POST、载荷为空', async () => {
    await markNotificationRead(7)
    expect(calls[0]).toEqual({
      method: 'post',
      url: '/api/business/notifications/7/read/',
    })
    // 后端没有批量端点（`urls.py:55-64` 只有 list + read 两条），所以导出面里
    // **不允许**出现 `/read-all/`、`/read/`（集合级）这类形状。
    expect(calls.filter((c) => c.url?.includes('read-all'))).toHaveLength(0)
  })
})
