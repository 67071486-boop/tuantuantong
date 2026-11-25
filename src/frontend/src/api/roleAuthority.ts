// 角色职位权限接口。契约来源：`src/apps/rbac/views.py` / `urls.py`（实现即契约），
// 端点表见 docs/specs/2026-09-20-rbac-permission-system-design.md §7。
//
// 与其它 api/*.ts 同一边界：本文件不 import 任何 UI 库。
//
// ⚠️ 三个后端口径（写错会静默错，不是 500）：
//  1. 列表**不分页**（对标 `page:false`）⇒ 响应 `data` 就是数组，不是 `Page<T>`；
//  2. `copy` 的载荷只有 `{name, remark}`，权限**与「可作为销售客服」标记**都从**源角色**
//     整份抄（`views.py` 的 `copy`）；
//  3. 更新一律用 **PATCH**：`PUT` 是全量替换，缺 `remark` 会**清空**（`views.py` 的 `update`）。
//     本线**四个**字段每次都全带，PATCH 与 PUT 结果相同，但 PATCH 不会在将来漏字段时清数据。
//     ⚠️ 引用一律按**函数/类名**（`views.py::update` 这种），⛔ 不写行号：行号会随任何一次改动腐化
//     （本文件自己先前那几处 `views.py:<行号>` 就是这么过期的）。
import { request } from './http'

const BASE = '/api/role-authorities/'

/** 列表一行（`views.py::list`）：形状恰好是 `{id,name,remark,can_be_sales_agent}` 四个字段。 */
export type RoleAuthorityRow = {
  id: number
  name: string
  remark: string
  /** 「可作为销售客服」（`Role.can_be_sales_agent`，2026-10-08 本线新增）：持这个标记的角色的人才进录单页「接单销售客服」的候选（`selectors.kefu_options`）。⚠️ 列表页**不消费**它（今天只在详情回填那颗勾选框时用）——它进列表载荷是为了让写面（`RoleWritePayload` 必带）与读面成对，⛔ 不是给列表多加一列。 */
  can_be_sales_agent: boolean
}

/** 详情（`views.py::_detail`）。`permission_codes` 后端已排序。 */
export type RoleAuthorityDetail = {
  id: number
  name: string
  remark: string
  permission_codes: string[]
  /** 编辑态回填那颗勾选框用的**唯一**来源（没有它，保存会把已勾上的标记改掉）。 */
  can_be_sales_agent: boolean
  dashboard_type?: DashboardType
}

export type DashboardType = '' | 'worker' | 'work'

/** 权限清单 5 列（`selectors.py::role_permission_rows`）。
 * ⚠️ 顶级节点的 `parent_name` 是字符串 **`无`**，不是 `null`（对标实测）。 */
export type RolePermissionRow = {
  name: string
  level: number
  parent_name: string
  note: string
  order_by: number
}

/** 目录树的一个节点（栏目与叶子同形；`selectors.py::node_tree`）。 */
export type PermissionNodeLeaf = {
  code: string
  name: string
  note: string
  level: number
  /** ⚠️ **栏目行（`level=0`）的 `available` 恒为 `False`，没有信息量**
   * （spec §5.3 追加之二）⇒ 只用它决定**叶子**要不要提示「尚未开放」。 */
  available: boolean
}

export type PermissionNodeModule = PermissionNodeLeaf & { children: PermissionNodeLeaf[] }

/** 新建/编辑的写载荷（`RoleCreateSerializer` / `RoleUpdateSerializer`）。 */
export type RoleWritePayload = {
  name: string
  remark: string
  permission_codes: string[]
  /** ⭐ **对标没有这一格**（2026-10-08 本线新增，见 `docs/DECISIONS.md` 本线那条决策）：
   * 「可作为销售客服」。**必带**（不是可选）——`PATCH` 的缺席语义是"保持库里的值"，
   * 而本页的表单每次都整份提交，漏带会让取消勾选**静默不生效**。 */
  can_be_sales_agent: boolean
  /** 旧版编辑页不提供此项，PATCH 缺省时保持原值。 */
  dashboard_type?: DashboardType
}

/** 复制载荷（`RoleCopySerializer`）：**刻意不含** `permission_codes`，**也不含**`can_be_sales_agent`——复制动作的语义是"新角色 = 源角色的整份拷贝"，权限与标记都从**源角色**抄（`views.py` 的 `copy`），⛔ 别在这里加一个"载荷优先"的口子（那会让两侧中的一侧被静默忽略）。 */
export type RoleCopyPayload = { name: string; remark: string }

export function listRoleAuthorities(): Promise<RoleAuthorityRow[]> {
  return request<RoleAuthorityRow[]>({ method: 'get', url: BASE })
}

export function getRoleAuthority(id: number): Promise<RoleAuthorityDetail> {
  return request<RoleAuthorityDetail>({ method: 'get', url: `${BASE}${id}/` })
}

export function createRoleAuthority(payload: RoleWritePayload): Promise<RoleAuthorityDetail> {
  return request<RoleAuthorityDetail>({ method: 'post', url: BASE, data: payload })
}

export function updateRoleAuthority(
  id: number,
  payload: RoleWritePayload,
): Promise<RoleAuthorityDetail> {
  return request<RoleAuthorityDetail>({ method: 'patch', url: `${BASE}${id}/`, data: payload })
}

export function deleteRoleAuthority(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${BASE}${id}/` })
}

export function copyRoleAuthority(
  id: number,
  payload: RoleCopyPayload,
): Promise<RoleAuthorityDetail> {
  return request<RoleAuthorityDetail>({
    method: 'post',
    url: `${BASE}${id}/copy/`,
    data: payload,
  })
}

export function fetchRolePermissionRows(id: number): Promise<RolePermissionRow[]> {
  return request<RolePermissionRow[]>({ method: 'get', url: `${BASE}${id}/permissions/` })
}

export function fetchPermissionTree(): Promise<PermissionNodeModule[]> {
  return request<PermissionNodeModule[]>({ method: 'get', url: '/api/permission-nodes/' })
}
