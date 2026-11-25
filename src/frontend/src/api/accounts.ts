// 账号（系统人员）接口。契约来源：`src/apps/accounts/`（实现即契约）——
// 端点表见 `docs/specs/2026-09-20-account-management-design.md` §5。
//
// 与 `api/config.ts` 同一边界：本文件不 import 任何 UI 库；`request` 在 `http.ts`、
// `Page<T>` 在 `unwrap.ts`。
import { request } from './http'
import type { Page } from './unwrap'

/** 职位名。后端 `/api/accounts/roles/` 回的是**当前租户的全部角色名**（内置职位 + 租户自建），形状仍是 `string[]`；判据是**请求期**的 `Role` 存在性校验。⚠️ **别在这里写死内置职位的个数**（原文写「内置 5 + 自建」）：内置清单的权威出处是`apps/rbac/seed.py::BUILTIN_ROLE_MODULES`，与供给时的 `apps/business/roles.py::DEFAULT_ROLES`不是同一份清单（线 D 起前者多一个内置「人事」）。 */
export type AccountRole = string

/** 档案字段（`StaffProfile` 的对外列，`apps/accounts/payloads.py::_profile_fields`）。⚠️ **全部可选**：`auth.User` 没有档案行时（`seed_tenant` 灌的 `admin`）这批键**整个不出现**，不是「给出模型默认值」。消费方按可选处理。金额/日期一律是**字符串**——DRF `DecimalField` 默认 `COERCE_DECIMAL_TO_STRING=True`。 */
export type AccountProfile = {
  id_card_no?: string
  business_type?: number
  member_type?: number
  is_studio?: boolean
  /** 「开户人」显示名（`StaffProfile.opened_by` 经后端昵称优先口径，只读——写入点在
   * 创建账号/自助注册两条路径，`PATCH` 盖不到它）。⚠️ 没有档案行时键整个缺席、开户人被硬删（SET_NULL）时是空串。 */
  createUserName?: string
  base_salary?: string | null
  commission_mode?: number
  commission_value?: string
  alipay_account?: string
  alipay_name?: string
  bank_name?: string
  bank_card_no?: string
  grab_limit_dl?: number
  grab_limit_pw?: number
  grab_price_min?: string | null
  grab_price_max?: string | null
  /** 「禁自助接单」（`StaffProfile.grab_forbid_self_service`，P3e Task 1 / D-0091）。
   * 列表与详情都带它；⚠️ 判值用 `=== true`——缺档案行时这个键**整个不出现**（不是 `false`）。 */
  grab_forbid_self_service?: boolean
  /** 「禁止结算」（`StaffProfile.settle_forbidden`，钱⑤ Task 2 / 业务方裁定）。与上一行同形：列表与详情都带它，`PATCH /api/accounts/<pk>/` 可写；列表页「禁止结算」列的开关读的就是它。⚠️ 判值用 `=== true`——缺档案行时这个键**整个不出现**（不是 `false`）。 */
  settle_forbidden?: boolean
  live_url?: string
  qq?: string
  mobile?: string
  hired_on?: string | null
  tag?: string
  remark?: string
  deposit_amount?: string
  /** 「启用个人押金额度」总闸（`StaffProfile.deposit_quota_enabled`，默认 `false`）。⚠️ **只读**：写入点只有 `PUT /api/business/takers/<id>/deposit-quota/`（`api/takerDepositQuota.ts::setDepositQuotaEnabled`），⛔ 不在 `AccountWritePayload` 里⇒ `PATCH /api/accounts/<pk>/` 收不到它（钱相关的写入点保持一条）。人员管理页那一列的行内开关读它（只有持 `finance.taker_wallet_adjust` 的人才看得到那一列）。⚠️ 判值用 `=== true`——缺档案行时这个键**整个不出现**（不是 `false`）。 */
  deposit_quota_enabled?: boolean
  /** 「排序值」（`StaffProfile.order_by`，`PositiveIntegerField(default=0)`）。`0` 是合法且常见的值。 */
  order_by?: number
  /** 「删除时间」（`StaffProfile.deleted_at`）。**有值 = 这个人在回收站里**。⚠️ 默认列表**看不到**已删的人（后端 `base_queryset()` 默认排除），只有把「有效状态」筛到「已删除」（`AccountListParams.deleted = true`）才看得到它们；操作列据此只渲染「恢复」。`null` = 没删（后端对有档案行的人明确回 `null`）。⚠️ 它**不是可写字段**：`PATCH` 带它会回业务码 1000（后端在 `SERVER_MANAGED_FIELDS` 里），删除/恢复只走 `deleteAccount` / `restoreAccount` 两个 POST 端点。 */
  deleted_at?: string | null
}

/** 列表/详情的一行。列表里 `id_card_no` / `bank_card_no` / `alipay_account` 已**脱敏**。 */
export type AccountRow = {
  id: number
  username: string
  display_name: string
  is_active: boolean
  is_tenant_admin: boolean
  role: AccountRole | null
  games: number[]
  last_login: string | null
  date_joined: string
} & AccountProfile

/** 列表筛选（后端 `AccountFilterSerializer`）。空串按「没传」处理，所以可选参数不传即可。 */
export type AccountListParams = {
  page?: number
  page_size?: number
  role?: AccountRole
  is_active?: boolean
  key?: string
  opened_by?: number
  /** 业务类型（后端 `AccountFilterSerializer.business_type`，`BusinessType` 的**整数**取值域）：`0` 代练+陪玩 / `1` 专做代练 / `2` 专做陪玩。⚠️ 指派弹窗**只在选了 `1`/`2` 时才送**（`0` 那一档对标的语义是「代练+陪玩」这一档，而我们的 `business_type` 是**人身上的单选属性** ⇒ 送 `0` 会只留「标了代练+陪玩」的人，见 spec §7.1 的 C16 代价 ③）。 */
  business_type?: number
  /** 「已删除」（后端 `AccountFilterSerializer.deleted`）。
   * `true` ⇒ 只看已删的人（回收站口径）；不传 ⇒ 默认口径（**排除**已删）。
   * ⚠️ 它与 `is_active` **不是一回事**：`is_active:false` 仍然只看「禁用但没删」的人。 */
  deleted?: boolean
}

/** 写载荷（新增 `partial=false` / 编辑 `partial=true` 共用）。**服务端管理字段**（`password` / `is_superuser` / `is_staff` / `is_active`）**不在本类型里**：后端 `SERVER_MANAGED_FIELDS` 收到即拒（1000），不是静默忽略。改密只走 `resetAccountPassword`。金额字段允许 `number`（Element Plus 的 `el-input-number` 给的就是 number）或字符串；后端 `DecimalField` 两者都收。**一律 `>= 0`**（薪酬/限额，不是订单金额，不适用 D-0015）。 */
export type AccountWritePayload = {
  username: string
  display_name?: string
  role: AccountRole
  games: number[]
  id_card_no?: string
  business_type?: number
  member_type?: number
  is_studio?: boolean
  base_salary?: number | string | null
  commission_mode?: number
  commission_value?: number | string
  alipay_account?: string
  alipay_name?: string
  bank_name?: string
  bank_card_no?: string
  grab_limit_dl?: number
  grab_limit_pw?: number
  grab_price_min?: number | string | null
  grab_price_max?: number | string | null
  /** 「禁自助接单」。后端收它且无默认值：PATCH 缺席 = 保持库里的值。
   * 字段必须真在序列化器里。没声明时 DRF 会静默丢掉这个键，接口仍回成功、库不变。 */
  grab_forbid_self_service?: boolean
  /** 「禁止结算」。与上一行同一个写法：后端 `AccountWriteSerializer` 收它
   * （`BooleanField(required=False)`、无默认值；PATCH 时缺席 = 保持库里的值）。
   * ⚠️ `false` 是**合法写入值**（= 解除禁止），不是「没传」——别用 `if (value)` 判。 */
  settle_forbidden?: boolean
  live_url?: string
  qq?: string
  mobile?: string
  hired_on?: string | null
  tag?: string
  remark?: string
  deposit_amount?: number | string
  /** 「排序值」。可写：后端 `apps/accounts/serializers.py` 的 `order_by`（`min_value=0, max_value=9999`，
   * `required=False`）收它——列表页「排序值」列的点内编辑就是靠这个字段 PATCH。
   * 上限 9999（不是 `PositiveIntegerField` 的默认上限），所以提示框的 `maxlength` 是 4。 */
  order_by?: number
}

const BASE = '/api/accounts/'

/** 列表。`data` 形状 = `EnvelopePagination` 的 `{items,total,page,page_size}`。 */
export function listAccounts(params: AccountListParams = {}): Promise<Page<AccountRow>> {
  return request<Page<AccountRow>>({ method: 'get', url: BASE, params })
}

export function fetchAccountOpeners(q = '', page = 1): Promise<{ items: Array<{ id: number; name: string; username: string }>; total: number; page: number }> {
  return request({ method: 'get', url: `${BASE}openers/`, params: { q, page } })
}

/** 详情（编辑回填用；**不持限制型 flag 时 PII 全量**，与列表 `mask_pii` 的脱敏口径不同）。
 * 持 `system_user.restrict_view_id_card` / `restrict_view_commission_params` 时，
 * 详情与写响应同列表一样被遮——那是**另一层**，判据在后端 `payloads.py::account_payload(viewer=)`。 */
export function getAccount(id: number): Promise<AccountRow> {
  return request<AccountRow>({ method: 'get', url: `${BASE}${id}/` })
}

/** 职位下拉数据源。 */
export function fetchAccountRoles(): Promise<AccountRole[]> {
  return request<AccountRole[]>({ method: 'get', url: `${BASE}roles/` })
}

/** 新增。口令由**服务端生成**，只在这次响应里出现一次（`initial_password`）。
 * 调用方必须把显示做到位——丢了响应就再也拿不到它。 */
export function createAccount(
  payload: AccountWritePayload,
): Promise<{ account: AccountRow; initial_password: string }> {
  return request<{ account: AccountRow; initial_password: string }>({
    method: 'post',
    url: BASE,
    data: payload,
  })
}

/** 编辑。请求体**不接受**密码与启停字段（那条判据在类型上就挡住了）。 */
export function updateAccount(
  id: number,
  payload: Partial<AccountWritePayload>,
): Promise<AccountRow> {
  return request<AccountRow>({ method: 'patch', url: `${BASE}${id}/`, data: payload })
}

/** 单项启停。**幂等 set**（不是对标物的 toggle）：重复发同样载荷结果不变。 */
export function setAccountStatus(id: number, isActive: boolean): Promise<{ updated: number[] }> {
  return request<{ updated: number[] }>({
    method: 'post',
    url: `${BASE}${id}/status/`,
    data: { is_active: isActive },
  })
}

/** 批量启停（整批原子：任一 id 不存在则整批不落库）。上限 200（后端 `max_length=200`）。 */
export function setAccountsStatus(
  ids: number[],
  isActive: boolean,
): Promise<{ updated: number[] }> {
  return request<{ updated: number[] }>({
    method: 'post',
    url: `${BASE}status/`,
    data: { ids, is_active: isActive },
  })
}

/** 重置密码为后端固定常量 `123456`。响应**不回显**口令（它只在新增响应里出现一次）。 */
export function resetAccountPassword(id: number): Promise<{ id: number }> {
  return request<{ id: number }>({ method: 'post', url: `${BASE}${id}/reset-password/` })
}

/** **软删**一个账号：`POST /api/accounts/<id>/delete/`，**无 body**。⚠️ 这不是硬删：后端只写 `deleted_at` + 把 `is_active` 置 `False`（硬删在数据层做不了——`Performance.user` / `Order.sales_user` 都是 `PROTECT`）。可恢复，见 `restoreAccount`。⚠️ **幂等**：已删的再调返回成功、不重复写留痕。⚠️ 自己被拒（业务码 1004）、最后一个活跃管理员被拒（1003）——由调用方展示 `msg`。 */
export function deleteAccount(id: number): Promise<{ id: number }> {
  return request<{ id: number }>({ method: 'post', url: `${BASE}${id}/delete/` })
}

/** **恢复**一个已软删的账号：`POST /api/accounts/<id>/restore/`，**无 body**。⚠️ 「恢复」≠「启用」：后端**不**碰 `is_active` ⇒ 恢复后这个人仍然登录不了，要启用得另外点「禁用/启用」（两个动作各自语义单一）。⚠️ **幂等**：没删过的再调返回成功、不写留痕。 */
export function restoreAccount(id: number): Promise<{ id: number }> {
  return request<{ id: number }>({ method: 'post', url: `${BASE}${id}/restore/` })
}
