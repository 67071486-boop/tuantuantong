// 系统内部公告接口。契约来源（实现即契约）：
// `src/apps/business/api/notice_views.py` + `src/apps/business/urls.py` 的
// `router.register("system-notices", …)`。
//
// 端点：
// - `GET /api/business/system-notices/`（登录即可；`page` / `page_size`）
// - `POST /api/business/system-notices/`（`system.publish_notice`，只收 `content`）
// - `PATCH /api/business/system-notices/<id>/`（同上，只收 `content`）
// - `DELETE /api/business/system-notices/<id>/`（同上）
//
// ⚠️ `title` 由服务端从 `content` 派生（首行 + 截 50 字），本文件**不传**它。
import { request } from './http'
import type { Page } from './unwrap'

export type SystemNotice = {
  id: number
  title: string
  content: string
 /** 发布人用户名（服务端取认证态）。 */
  created_by: string
  created_at: string
  updated_at: string
}

export const SYSTEM_NOTICE_PATH = '/api/business/system-notices/'

/** 后端分页上限（`EnvelopePagination.max_page_size = 300`）。 */
export const SYSTEM_NOTICE_PAGE_SIZE = 200

export function fetchSystemNotices(
  params: { page?: number; page_size?: number } = {},
): Promise<Page<SystemNotice>> {
  return request<Page<SystemNotice>>({ method: 'get', url: SYSTEM_NOTICE_PATH, params })
}

export function createSystemNotice(content: string): Promise<SystemNotice> {
  return request<SystemNotice>({
    method: 'post',
    url: SYSTEM_NOTICE_PATH,
    data: { content },
  })
}

export function updateSystemNotice(id: number, content: string): Promise<SystemNotice> {
  return request<SystemNotice>({
    method: 'patch',
    url: `${SYSTEM_NOTICE_PATH}${id}/`,
    data: { content },
  })
}

export function deleteSystemNotice(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${SYSTEM_NOTICE_PATH}${id}/` })
}
