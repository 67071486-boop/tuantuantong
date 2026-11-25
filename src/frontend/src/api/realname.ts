// 实名认证（人员实名记录与配置页）接口。
// 契约来源 = **后端实现本身**（`src/apps/accounts/views.py` 的四个 `realname*` action +
// `src/apps/accounts/serializers.py` 的两个序列化器 + `src/apps/realname/models.py` 的两个枚举），
// spec 见 `docs/specs/2026-10-10-realname-verification-design.md` §9.1 / §10.1。
//
// 与 `api/accounts.ts` 同一边界：本文件不 import 任何 UI 库；`request` 在 `http.ts`、
// `Page<T>` 在 `unwrap.ts`。
import { request } from './http'
import type { Page } from './unwrap'

/** 四个端点都在 `AccountViewSet` 上（`@action(url_path=…)`），基础路径与 `api/accounts.ts` 同。 */
const BASE = '/api/accounts/'

/** 「实名结果」筛选的取值域（**字符枚举**，不是整数）。⚠️ 参数名是 **`status`**、取值 `'0' | '1' | '2'` —— 对标**取数参数**就是 `status`，而它页内控件的 `name` 是 `certStatus`（证据边界 `docs/JIDANPU-REFERENCE.md:1078`）。后端 `RealNameLogFilterSerializer.status` 是 `ChoiceField(choices=("0","1","2"))`⇒ ⛔ 不要照控件名写成 `cert_status`，也⛔ 不要当成整数传。⚠️ **`'0'` = 「全部」而不是「没传」**（后端用 `is not None` 判「传没传」，`AGENTS.md` §5-E / E-0021：`0` 是合法且有意义的边界）⇒ 选了「全部」时本模块**照样把它发出去**，由后端 `REALNAME_STATUS_TO_RESULT['0'] is None` 那一支决定「不过滤」。 */
export const REALNAME_STATUS_ALL = '0'
/** 筛选「实名失败」（后端 `REALNAME_STATUS_TO_RESULT['1'] == CertResult.FAIL`）。 */
export const REALNAME_STATUS_FAIL = '1'
/** 筛选「实名通过」（后端 `REALNAME_STATUS_TO_RESULT['2'] == CertResult.PASS`）。 */
export const REALNAME_STATUS_PASS = '2'

/** 认证方式（`apps/realname/models.py::CertMethod` 的**存储值**，`TextChoices`）。
 * ⚠️ 不是对标的 `id5` 整数 `1`/`2` —— 那个映射在列渲染那一侧（`certMethodBadge`）。 */
export type RealNameCertMethod = 'basic' | 'face'
/** 认证结果（`apps/realname/models.py::CertResult` 的存储值）。 */
export type RealNameCertResult = 'pass' | 'fail'

/** 列表行（= 后端 `_realname_log_payload` 的**逐键**形状，`apps/accounts/views.py`）。⚠️ **`id_card_no` 是脱敏后的值**（后端复用 `payloads.py::_mask_pii`，前 3 后 2）——列上那句「不予完整展示」就是这个事实的文案，⛔ 别在前端再脱敏一次。⚠️ **没有** `vendor_code` / `vendor_message` / `vendor_request_id` / `package_remark`：后端那份载荷**不含**它们（它们在库里，但不出现在这个读面上）。⚠️ `age` 是**提交当日**算出的年龄、`null` 表示当时没算出来（后端 `null=True`）——⛔ 别把 `null` 当 `0`：那会让 `0` 岁的行也吃到「年龄异常」的红字口径。 */
export type RealNameLogRow = {
  id: number
  /** 「系统用户名称」列 = 提交那一刻的**登录账号快照**（`user_name_snapshot`）。 */
  user_name: string
  /** 「真实姓名」列 = `name`。 */
  name: string
  /** 「身份证号」列 = **脱敏后**的 `id_card_no`。 */
  id_card_no: string
  /** 「性别」列 = 从身份证号**本地解析**（厂商那条接口不返回它，spec §4.2）。 */
  gender: string
  /** 「年龄」列 = 提交当日算出的年龄，`null` = 没算出来。 */
  age: number | null
  /** 「认证方式」列 = `CertMethod`。 */
  cert_method: RealNameCertMethod
  /** 「认证结果」列 = `CertResult`。 */
  result: RealNameCertResult
  /** 「申请时间」列 = ISO-8601 串（`applied_at`）。 */
  applied_at: string
  /** 「认证时间」列 = ISO-8601 串（`certified_at`），`null` = 还没拿到厂商结果。 */
  certified_at: string | null
  /** 「认证图片」列 = `cert_image`。⚠️ **本批恒空**（基础认证/二要素没有图，spec §13-2-4）
   * ⇒ 列照渲染、点了提示「暂无认证图片」，⛔ **不置灰**（`AGENTS.md` §5-D）。 */
  cert_image: string
}

/** 列表筛选（= 后端 `RealNameLogFilterSerializer` 的逐字段）。⚠️ **`status` 是字符串枚举**（见 `REALNAME_STATUS_*`）；`sdate` / `edate` 是 `YYYY-MM-DD`（后端 `DateField`，畸形日期在**入口**被拒成 200 + code 1000，不会带着字符串进 `filter()`）。⚠️ `key3`（身份证号）后端 `max_length=18` —— 与证据件那个输入框的 `maxlength="18"` 同值。⭐ **两段式取数**（对标 `RnameCertLogAndSetting.js:27,35` vs `:151-154`）：**首次载入只发 `sdate`/`edate`**，`status`/`key1`/`key2`/`key3` **只在点「搜索」后**才拼进去。⇒ 本模块**不**在取数层做这件事（那是页面状态机的职责）：调用方首屏传 `buildRealNameLogParams`
 * 的 `includeFilters: false` 那一档，点搜索后传 `true`。 */
export type RealNameLogParams = {
  /** 实名时间起（`YYYY-MM-DD`，含当天；后端按 `applied_at` 过滤）。 */
  sdate?: string
  /** 实名时间止（`YYYY-MM-DD`，含当天）。 */
  edate?: string
  /** 实名结果：`'0'` 全部 / `'1'` 失败 / `'2'` 通过（**字符枚举**）。 */
  status?: string
  /** 系统用户名称（包含匹配）。 */
  key1?: string
  /** 真实姓名（包含匹配）。 */
  key2?: string
  /** 身份证号（包含匹配，后端 `max_length=18`）。 */
  key3?: string
  page?: number
  /** 每页条数。后端 `RealNameLogPagination` 的**默认与上限都是 20**（对标 `limits: [20]`）。 */
  limit?: number
}

/** `RealNameLogParams` → 线上参数（**唯一形状出口**）。⭐ `includeFilters` 就是那「两段」的开关：- `false`（首屏）⇒ 只放 `sdate` / `edate`（+ 分页）；- `true`（点过「搜索」）⇒ 再放 `status` / `key1` / `key2` / `key3`。⚠️ **判据是「空」而不是真值**（`AGENTS.md` §5-E / E-0021）：`status='0'` 是**真实取值**（「全部」），必须原样留下；`put` 只丢 `undefined` / `null` / **空串**。⛔ 别在这里另加一层「`'0'` 等于没选 ⇒ 丢掉」的归一化 —— 那会让后端收到「没传 status」而前端控件显示「全部」两处说法不一致（今天两者恰好同结果，但那是后端的巧合，不是契约）。 */
export function buildRealNameLogParams(
  params: RealNameLogParams,
  includeFilters: boolean,
): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  const put = (key: string, value: string | number | undefined | null): void => {
    if (value === undefined || value === null) return
    if (typeof value === 'string') {
      const trimmed = value.trim()
      if (trimmed === '') return
      out[key] = trimmed
      return
    }
    out[key] = value
  }
  put('sdate', params.sdate)
  put('edate', params.edate)
  if (includeFilters) {
    put('status', params.status)
    put('key1', params.key1)
    put('key2', params.key2)
    put('key3', params.key3)
  }
  put('page', params.page)
  put('limit', params.limit)
  return out
}

/** 列表：`GET /api/accounts/realname-logs/`（持节点 `system_user.realname_records`(1263)）。分页参数名是 **`page` / `limit`**（后端 `RealNameLogPagination.page_size_query_param = "limit"`），⛔ **不是** `EnvelopePagination` 的 `page_size` —— 与账号列表（`api/accounts.ts`）不同名，别把两页的参数模板混用（`AGENTS.md` §5-E / E-0007 同族）。 */
export function listRealNameLogs(
  params: RealNameLogParams,
  includeFilters: boolean,
): Promise<Page<RealNameLogRow>> {
  return request<Page<RealNameLogRow>>({
    method: 'get',
    url: `${BASE}realname-logs/`,
    params: buildRealNameLogParams(params, includeFilters),
  })
}

/** 那四项配置的读 / 写形状（= 后端 `_realname_setting_payload` 的逐键，**照库里的真值**）。⚠️ 后端**不在这里套默认值**（默认值只在 `RealNameSetting` 的列默认值那一处定义：三个布尔 `False`、两个年龄 `0`）⇒ 本类型如实照抄那个形状。⛔ 不含 `key_id` / `key_secret`（那两个字段压根不在这张表上）。 */
export type RealNameSetting = {
  /** 对标 `CertSwicth`：是否启用子账号实名认证（默认 `false` = 不启用）。 */
  enabled: boolean
  /** 对标 `CertPassRecOrder`：是否不允许未实名子账号在内部抢单大厅接单（默认 `false` = 允许）。 */
  block_claim_without_cert: boolean
  /** 对标 `CertOnineSett`：在线结算打款是否要求已实名（默认 `false` = 不要求）。 */
  require_cert_for_settlement: boolean
  /** 对标 `CertMinAge`：允许通过实名的**最小**年龄，`0` = **不限制**（合法值，⛔ 不是「没填」）。 */
  min_age: number
  /** 对标 `CertMaxAge`：最大年龄，`0` = 不限制。 */
  max_age: number
}

/** 写载荷 = `RealNameSetting` 的**部分**更新（后端 `RealNameSettingWriteSerializer` + `partial=True`）。 */
export type RealNameSettingPatch = Partial<RealNameSetting>

/** 读那四项配置：`GET /api/accounts/realname-setting/`（持 1263；**没有就按默认值建一行**）。
 * 配置弹窗回填用。 */
export function getRealNameSetting(): Promise<RealNameSetting> {
  return request<RealNameSetting>({ method: 'get', url: `${BASE}realname-setting/` })
}

/** 写那四项配置：`PATCH /api/accounts/realname-setting/`（持 1263）。⚠️ **`PATCH` 是部分更新**：未传的键后端**保持原值**（它用逐键 `in validated_data` 判断，⛔ 不是 `data.get(键, 默认值)`）⇒ 调用方只发**真的要改**的键即可，但也**必须**把要改的键都带上（漏带 = 那次修改静默不生效）。 */
export function updateRealNameSetting(payload: RealNameSettingPatch): Promise<RealNameSetting> {
  return request<RealNameSetting>({ method: 'patch', url: `${BASE}realname-setting/`, data: payload })
}

/** 本租户**剩余核验次数**的形状（= 后端 `realname_quota` 的返回）。⚠️ `remaining` 可以是 **`null`**：那是「**跨 schema 读不到**」这一档（`apps/realname/views.py::remaining_quota` 只在这一档回 `None`，**⛔ 不许静默当成 0**——那会让用户以为「我没次数了」去催客服购买，而其实是我们读不到）。此时 `msg` 带一句人话。⚠️ `remaining === 0` 是**合法读数**（没买过 / 买过用完）⇒ 判「读没读到」用 `!== null`，⛔ 不用真值判断（`AGENTS.md` §5-E / E-0021）。 */
export type RealNameQuota = {
  remaining: number | null
}

/** 本租户剩余次数：`GET /api/accounts/realname-quota/`（持 1263；供购买按钮的文案）。 */
export function getRealNameQuota(): Promise<RealNameQuota> {
  return request<RealNameQuota>({ method: 'get', url: `${BASE}realname-quota/` })
}

/** 配置按钮的**文案模板**（spec §2.4：`配置实名资源包(当前余量N次)`）。原文案是证据件里的「购买实名认证套餐包(当前余量0次)」（对标站外购买那颗）；业务方拍板改为**应用内配置**（按钮开配置弹窗）⇒ 文案换成「配置实名资源包」，N 的取值链不动（`GET /api/accounts/realname-quota/`）。⚠️ 括号是**半角** `(` `)`（沿用证据件那颗按钮的形状），⛔ 不要写成全角。⚠️ 读不到（`null`）时**不假装是 0**：那一档后端已经给了人话 msg，页面用它提示；按钮先回落到字面量 `0`。⛔ 不要写成 `remaining || 0`：那会把合法的 `0` 与 `null` 混成一个（`E-0021`）。 */
export function buyButtonLabel(remaining: number | null): string {
  const n = remaining === null ? 0 : remaining
  return `配置实名资源包(当前余量${n}次)`
}

/* ────────────────────────── 实名资源包（spec §2.4） ────────────────────────── */

/** 一行**掩码后**的密钥包（= 后端 `package_admin.package_payload` 的逐键形状）。
 * ⛔ **没有** `key_secret` 这个键——secret 只写不读，任何响应都不回显（判据同命令）；
 * `key_id` 是后端掩码后的值（前 4 位 + `***`），⛔ 前端不再二次加工。 */
export type RealNamePackageRow = {
  id: number
  key_id: string
  total_quota: number
  used_quota: number
  expires_at: string | null
  is_active: boolean
  remark: string
  created_at: string
}

/** 添加密钥包的载荷（后端校验照命令，`package_admin.add_package` 单一出处）。 */
export type RealNamePackageCreatePayload = {
  key_id: string
  key_secret: string
  total_quota: number
  /** `YYYY-MM-DD`（后端解析；不给 = 不过期）。 */
  expires_at?: string
  remark?: string
}

/** 现有密钥包列表：`GET /api/accounts/realname-packages/`（持 1263）。 */
export function listRealNamePackages(): Promise<RealNamePackageRow[]> {
  return request<RealNamePackageRow[]>({ method: 'get', url: `${BASE}realname-packages/` })
}

/** 添加一个密钥包：`POST /api/accounts/realname-packages/`（持 1263）。⚠️ **secret 只在这一条请求上出现**（进库即不出）——⛔ 不写日志、不发往别的端点、任何响应都不回显（后端根本不回，前端也别试图把它存下来）。失败（重复 key-id / 总次数 < 1 / 有效期解析不了……）一律抛 **BizError**，文案是**后端那句**（与 `realname_package` 命令同源），⛔ 页面不新造判据。 */
export function createRealNamePackage(
  payload: RealNamePackageCreatePayload,
): Promise<RealNamePackageRow> {
  return request<RealNamePackageRow>({
    method: 'post',
    url: `${BASE}realname-packages/`,
    data: payload,
  })
}

/** 用户自助核验的**提交载荷**（= 后端 `RealNameVerifySerializer` 的两个字段，逐字同名）。⚠️ 字段名是 **`name`** / **`id_card_no`**（⛔ 不是证据件那两个控件名 `certName` / `certSid`——那两个是**页内 DOM 的 name**，与线上载荷是两件事，见 `AGENTS.md` §5-C「看它值流去哪个端点」）。⚠️ 姓名与号码**不在这里 trim**：后端序列化器已经 strip 后判非空，前端再抄一份就是两个出处。 */
export type RealNameVerifyPayload = {
  /** 真实姓名。 */
  name: string
  /** 身份证号（明文**只在这条请求上出现**，落库为明文是后端已登记的残差，spec §7）。 */
  id_card_no: string
}

/** 核验的**返回形状**（= 后端 `RealNameVerifyView.post` 里那个三键载荷）。⚠️ **只有脱敏号**（`id_card_masked`，前 3 后 2）：后端**绝不回**完整号码（`apps/realname/views.py::mask_id_card`）⇒ ⛔ 不许拿它当"回显用户填的号"用。⚠️ `remaining` 与 `GET /api/accounts/realname-quota/` 同口径，可以是 **`null`**（跨 schema 读不到）——判「读没读到」用 `!== null`，⛔ 不用真值判断（`E-0021`）。 */
export type RealNameVerifyResult = {
  /** 这次核验的结果：`pass` 通过 / `fail` 没通过（不是"出错"——出错走业务码）。 */
  result: RealNameCertResult
  /** 脱敏后的身份证号（如 `110***00`）。 */
  id_card_masked: string
  /** 剩余核验次数，`null` = 读不到。 */
  remaining: number | null
}

/** 给自己核验一次：`POST /api/auth/realname/verify/`。⚠️ **路径不带 `accounts/`**：这个端点挂在 `apps/realname/urls.py`（`config/urls.py` 里`path("api/auth/realname/", …)`），与本文件其余四个 `api/accounts/…` 端点**不同前缀**——⛔ 别照着上面的 `BASE` 拼（那会打到 404 上）。⚠️ **失败一律抛 `BizError`**（`unwrap()` 的既有语义），业务码由**调用方**分辨：`1025` 未启用 / `1026` 本租户余量不足 / `1027` 平台无可用包 / `1000` 号码格式非法 /`1029` 提交过于频繁（D-0251，不锁定、窗口滚过自动恢复）。⛔ 本模块不把业务码翻成人话——那是**页面**的活（同一句话在不同页面可能不同）。⚠️ **计费口径**：这个调用**会消耗租户 1 次**（厂商按"查得"计费，一致**与不一致都算**，
 * spec §5 / §6-2b）⇒ 页面**必须在提交前**把这件事告诉用户（判据见页测试）。 */
export function verifyRealName(payload: RealNameVerifyPayload): Promise<RealNameVerifyResult> {
  return request<RealNameVerifyResult>({
    method: 'post',
    url: '/api/auth/realname/verify/',
    data: payload,
  })
}
