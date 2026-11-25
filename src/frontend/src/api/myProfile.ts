import { request } from './http'

/** 「我的资料」自助端点（`apps/core/profile_views.py` / `auth_views.py`，spec §5.1–§5.3）。 */
export type MyProfile = {
  username: string
  display_name: string
  /** 单值职位（名称升序首个）；无角色 ⇒ `''`。 */
  role: string
  id_card_no: string
  /** 派生：`id_card_no != ''`（服务端算的，前端不再自己判空）。 */
  is_real_name: boolean
  /** ⭐ 本租户的**实名开关** + **剩余核验次数**（后端 `apps/core/profile_views.py::_realname_summary`，spec `2026-10-10-realname-verification-design.md` §9 / §10.2，T7 已交付）。- `enabled`：只有启用了，子账号那边**才能发起**实名认证（对标配置页原话）⇒ 决定「我的资料」页那颗「点击进行实名认证」显示不显示（`user-myinfo.ts::renderRealName`）；- `remaining`：`null` = **读不到**（跨 schema 读不到计数），`0` = 确实一次都没有了—— 两者不是一回事（`E-0021`：边界判据用 `is not None`，⛔ 不用真值）。⚠️ **可选字段**（`?`）：老响应 / 平台平面可能**根本没有这个键** ⇒ 读取处一律写`profile.realname?.enabled ?? false`。⛔ 不许改成必填。 */
  realname?: { enabled: boolean; remaining: number | null }
  /** `Decimal` 由 DRF 编码 ⇒ 实际是 number 或 string（两种都兼容）。 */
  base_salary: number | string | null
  alipay_account: string
  alipay_name: string
  bank_name: string
  bank_card_no: string
  qq: string
  mobile: string
  live_url: string
  games: number[]
  /** 头像的**短时效读链接**（图片存储线，）。没传过 ⇒ `null`。
   * ⚠️ 服务端签发的 URL，⛔ 前端不自己拼；每次 GET 都会重新签。 */
  avatar_url: string | null
  /** 微信收款码的短时效读链接；没传过 ⇒ `null`。 */
  wechat_qr_url: string | null
}

/** 两种「用户媒体」（与后端 `profile_views.MEDIA_KINDS` 的白名单一一对应）。 */
export type MediaKind = 'avatar' | 'wechat-qr'

/** 要一张直传凭证（`POST /api/auth/profile/media/<kind>/ticket`）。 */
export function requestMediaTicket(
  kind: MediaKind, ext: string,
): Promise<{ key: string; upload_url: string; expires_in: number }> {
  return request({ url: `/api/auth/profile/media/${kind}/ticket`, method: 'POST', data: { ext } })
}

/** 认领刚直传上去的对象（`POST /api/auth/profile/media/<kind>`）。 */
export function claimMedia(kind: MediaKind, key: string): Promise<{ key: string }> {
  return request({ url: `/api/auth/profile/media/${kind}`, method: 'POST', data: { key } })
}

/** 自助可改的 7 个键（与后端 `EDITABLE_FIELDS` 一一对应）。 */
export type MyProfilePatch = Pick<
  MyProfile,
  'alipay_account' | 'alipay_name' | 'bank_name' | 'bank_card_no' | 'qq' | 'mobile' | 'live_url'
>

export function fetchMyProfile(): Promise<MyProfile> {
  return request<{ profile: MyProfile }>({ url: '/api/auth/profile', method: 'GET' })
    .then((data) => data.profile)
}

export function patchMyProfile(payload: Partial<MyProfilePatch>): Promise<MyProfile> {
  return request<{ profile: MyProfile }>({ url: '/api/auth/profile', method: 'PATCH', data: payload })
    .then((data) => data.profile)
}

export function changePassword(payload: {
  old_password: string
  new_password: string
  confirm_password: string
}): Promise<null> {
  return request<null>({ url: '/api/auth/password', method: 'POST', data: payload })
}
