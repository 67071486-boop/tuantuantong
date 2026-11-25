import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../http', () => ({ request: vi.fn() }))

import { request } from '../http'
import {
  copyRoleAuthority,
  createRoleAuthority,
  deleteRoleAuthority,
  fetchPermissionTree,
  fetchRolePermissionRows,
  getRoleAuthority,
  listRoleAuthorities,
  updateRoleAuthority,
  type RoleWritePayload,
} from '../roleAuthority'

const mocked = vi.mocked(request)

beforeEach(() => {
  mocked.mockReset()
  mocked.mockResolvedValue(undefined as never)
})

/** 契约来自 `src/apps/rbac/views.py` / `urls.py`（实现即契约），不是猜的。
 * 端点表见 spec §7。判据打在 URL / 方法 / 载荷上：路径写错、方法写成 `put`，
 * 都必须在这里红。 */
describe('角色权限接口的请求契约', () => {
  it('列表：GET /api/role-authorities/（**不带分页参数**——后端不分页）', async () => {
    await listRoleAuthorities()
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/role-authorities/' })
  })

  it('详情：GET /api/role-authorities/<id>/', async () => {
    await getRoleAuthority(7)
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/role-authorities/7/' })
  })

  it('新增：POST /api/role-authorities/，四字段原样带出（**含对标没有的 `can_be_sales_agent`**）', async () => {
    const payload: RoleWritePayload = {
      name: '客服主管',
      remark: '仅次于总经理',
      permission_codes: ['order.list', 'system_user'],
 // ⭐ 本线新增的第 4 个字段：不带上它时后端按 `default=False` 落库，
 // 勾选框等于**点了没用**（判据就是这一条：载荷必须与表单逐字段对齐）。
      can_be_sales_agent: true,
    }
    await createRoleAuthority(payload)
    expect(mocked).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/role-authorities/',
      data: payload,
    })
  })

  it('更新：PATCH（不是 PUT）——缺席即保持，避免把 remark 静默清空', async () => {
    const payload: RoleWritePayload = {
      name: '客服',
      remark: '',
      permission_codes: ['order.list'],
      can_be_sales_agent: false,
    }
    await updateRoleAuthority(3, payload)
    expect(mocked).toHaveBeenCalledWith({
      method: 'patch',
      url: '/api/role-authorities/3/',
      data: payload,
    })
  })

  it('删除：DELETE /api/role-authorities/<id>/', async () => {
    await deleteRoleAuthority(9)
    expect(mocked).toHaveBeenCalledWith({ method: 'delete', url: '/api/role-authorities/9/' })
  })

  it('复制：POST /api/role-authorities/<id>/copy/，载荷是 {name,remark}（**不含 permission_codes**，也不含 `can_be_sales_agent`）', async () => {
 // ⚠️ 两个键都**刻意不带**：权限**与「可作为销售客服」标记**都从**源角色**整份抄
 // （后端 `views.copy`）——前端若自作主张带一个，就会出现"载荷说了算还是源角色说了算"
 // 的第二份判据，且另一侧被静默忽略。
    await copyRoleAuthority(3, { name: '客服2', remark: '副本' })
    expect(mocked).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/role-authorities/3/copy/',
      data: { name: '客服2', remark: '副本' },
    })
  })

  it('权限清单：GET /api/role-authorities/<id>/permissions/', async () => {
    await fetchRolePermissionRows(3)
    expect(mocked).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/role-authorities/3/permissions/',
    })
  })

  it('目录树：GET /api/permission-nodes/', async () => {
    await fetchPermissionTree()
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/permission-nodes/' })
  })
})
