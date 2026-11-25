// 交易平台对接设置（计划 T3/T4）的出口。
//
// 分层与 `api/shopBindings.ts` / `api/xgjBindings.ts` 同形：传输、认证与信封解包全在
// `request()`（`./http.ts`）里，本文件不 import 任何 UI 库。
//
// ## 凭据红线（`AGENTS.md` §4）
//
// 后端对 `app_secret` / `pay_password` 是 `write_only`：**响应里没有这两个键**，
// 只有 `has_secret` / `has_password` 两个布尔。所以下面的读模型**没有**凭据字段——
// ⛔ 不要为了"方便核对"把它们加回 `PlatformIntegrationRow`：接口根本不回，加上去只会
// 得到一个恒 `undefined` 的字段，下一个读者会以为"后端回了但没渲染"。
import { request } from './http'

/** 集合路径（后端 `apps/business/urls.py` 的 `platform-integrations` router 注册）。 */
export const PLATFORM_INTEGRATIONS_URL = '/api/business/platform-integrations/'
/** 9 组全局设置的单例路径（同上，显式 `path()`）。 */
export const PLATFORM_INTEGRATION_SETTINGS_URL = '/api/business/platform-integrations/settings/'
/** 右面板摘要路径（实施计划 §4）。后端实现见 `DailiantongAccountSummaryView`（B6，与本分支同批）；读模型见`PlatformIntegrationSummary`。两边键名必须一致；对不上时页面会在「平台/账号/余额」那几格显示空白（⛔ 前端不会用假 0 兜底）。 */
export const PLATFORM_INTEGRATION_SUMMARY_URL =
  '/api/business/platform-integrations/summary/'

/** 一行平台对接账号（接口 snake_case；**不含任何凭据字面值**）。
 * `is_available` 只表示「该平台今天有没有客户端」（只有代练通为 `true`）——
 * **不是**「能不能存」：7 个平台都能存，另外 6 个只是本版不会真的拿去发单。 */
export type PlatformIntegrationRow = {
  id: number
  /** 平台 id（页面 7 个取值：10/30/80/90/93/50/60；代练通 = 10）。 */
  platform: number
  /** 平台中文名（后端 `get_platform_display()`，页面直接显示，不再维护第二份码表）。 */
  platform_label: string
  /** 平台账号 / ID。 */
  account: string
  /** AppId（不是密钥）。 */
  app_id: string
  /** **只回答「配没配」，不回答「配的是什么」**（后端 `app_secret` 是 `write_only`）。 */
  has_secret: boolean
  /** 支付密码配没配（后端只存 `MD5(密码)`，明文永不回显）。 */
  has_password: boolean
  is_enabled: boolean
  is_default: boolean
  sync_accept: boolean
  sms_notify: boolean
  include_push: boolean
  contact_qq: string
  contact_phone: string
  remark: string
  /** 该平台本版有没有真客户端（只有代练通为 true）。≠ 能不能存。 */
  is_available: boolean
  created_at: string
  updated_at: string
}

/** 新增 / 编辑的载荷。⚠️ **`app_secret` 省略或空串 = 不改**（后端 `update()` 的既有语义，照`XgjShopBindingSerializer` 的先例）；新增时省略 = 还没配置。⚠️ **`pay_password` 省略 = 不改**；但传**空串会被后端拒绝**（那是明确的写操作，不能变成静默 no-op）⇒ 页面只在用户填了值时才带这个键。 */
export type PlatformIntegrationPayload = {
  platform: number
  account: string
  app_id?: string
  app_secret?: string
  pay_password?: string
  is_enabled?: boolean
  is_default?: boolean
  sync_accept?: boolean
  sms_notify?: boolean
  include_push?: boolean
  contact_qq?: string
  contact_phone?: string
  remark?: string
}

/** 9 组全局设置的读模型（`GET /api/business/platform-integrations/settings/`）。⚠️ **两套值编码，不要强行归一**（后端 `PlatformPushSettings` 的字段注释）：`Y/N` 的 5 组落成布尔、`0/1` 的 4 组原值存整数；而且 `0` 有时表示「否 / 不限制 / 不发送」、有时表示「检测」——⛔ 不要按「0 = 关」的直觉取反。 */
export type PlatformPushSettings = {
  /** 1（SK132，`Y/N`）：录单页快速发单。 */
  quick_push_enabled: boolean
  /** 1.1（SK156，`0/1`）：传统简易模式（0 = 否）。 */
  legacy_simple_mode: number
  /** 1.2（SK157，`0/1`）：检测游戏大区准确性（0 = 否）。 */
  check_game_region: number
  /** 2（SK107，`Y/N`）：双金为 0 禁止发单。 */
  forbid_push_when_double_gold_zero: boolean
  /** 2.1（SK159，`0/1`）：重复发单检测（0 = 检测）。 */
  duplicate_push_check: number
  /** 3（SK106，`Y/N`）：默认勾选鸡蛋铺交易中心。 */
  select_jidanpu_center_by_default: boolean
  /** 4（SK145，`Y/N`）：撞单自动 0 撤。 */
  auto_zero_refund_on_conflict: boolean
  /** 5（SK155，`0/1`）：已完成订单不允许再发（0 = 不限制）。 */
  block_push_completed_orders: number
  /** 6（SK158，`0/1`）：平台接手后自动发群二维码（0 = 不发送）。 */
  auto_send_group_qr_after_accept: number
  /** 发单页「要求最低完单数」的全局默认（计划 §2.4）。
   * 后端 `PlatformPushSettings.default_power_level`，默认 **1**；`0` = 不设置
   * （对应发单页 `dlmmlimitAccept` 下拉的 `value="0"`）。 */
  default_power_level: number
  /** 发单页「拒绝新手和介入率」的全局默认（计划 §2.4）。
   * 后端 `PlatformPushSettings.default_limit_accept`，默认 **`null` = 不设置**
   * （页面下拉的 `value="0"` 也映射成 `null`，⛔ 不发一个「0%」的假阈值）。 */
  default_limit_accept: number | null
  /** 代练 / 陪玩要求的**唯一一份**全局模板（计划 §2.4）。
   * 后端 `PlatformPushSettings.require_template`，默认空串；发单时由后端写进 `Actors`
   * 第五段。页面只做「读出来预填 + 编辑保存」——⛔ 不做多模板。 */
  require_template: string
}

/** 列表：后端**直接回数组**（不是 `{items,total}`，见 `platform_integration_views.py` 文件头）。 */
export async function fetchPlatformIntegrations(): Promise<PlatformIntegrationRow[]> {
  return request<PlatformIntegrationRow[]>({ method: 'get', url: PLATFORM_INTEGRATIONS_URL })
}

/** 新增一个对接账号（POST）。 */
export async function createPlatformIntegration(
  payload: PlatformIntegrationPayload,
): Promise<PlatformIntegrationRow> {
  return request<PlatformIntegrationRow>({
    method: 'post',
    url: PLATFORM_INTEGRATIONS_URL,
    data: payload,
  })
}

/** 编辑（页面「修改」；后端只开 PATCH）。 */
export async function updatePlatformIntegration(
  id: number,
  payload: Partial<PlatformIntegrationPayload>,
): Promise<PlatformIntegrationRow> {
  return request<PlatformIntegrationRow>({
    method: 'patch',
    url: `${PLATFORM_INTEGRATIONS_URL}${id}/`,
    data: payload,
  })
}

/** 删除（硬删）。 */
export async function deletePlatformIntegration(id: number): Promise<null> {
  return request<null>({ method: 'delete', url: `${PLATFORM_INTEGRATIONS_URL}${id}/` })
}

/** 读 9 组全局设置（单行资源，首次读会懒建默认行）。 */
export async function fetchPlatformPushSettings(): Promise<PlatformPushSettings> {
  return request<PlatformPushSettings>({
    method: 'get',
    url: PLATFORM_INTEGRATION_SETTINGS_URL,
  })
}

/** 保存全局设置（`PUT`，后端 `partial=True` ⇒ 全量 / 部分都收）。调用方两种用法：- 设置页：一次发 9 组（`settingsRadioPayload`）；- 发单页：只发改动的键——`{default_power_level, default_limit_accept}` 或`{require_template}`。⛔ 不会把其它组覆盖成默认值（部分更新）。⚠️ 生效范围（2026-10-27 改准）：原 9 组里**只有第 6 条**（平台接手后自动发群二维码）真生效；另外 8 组仍只存不生效。新增的三列（`default_power_level` / `default_limit_accept` /`require_template`）**都真生效**（发单链路会读）。页面必须逐项标注，⛔ 不能再整块写「本版仅保存」。 */
export async function savePlatformPushSettings(
  payload: Partial<PlatformPushSettings>,
): Promise<PlatformPushSettings> {
  return request<PlatformPushSettings>({
    method: 'put',
    url: PLATFORM_INTEGRATION_SETTINGS_URL,
    data: payload,
  })
}

/** 发单页右面板的摘要（`GET …/platform-integrations/summary/`，计划 §4 / B6）。契约 = 后端 `dailiantong_push.platform_account_summary()` 的返回值（**键恰好这 7 个**，与后端 `B6` 的实现逐键一致；已按后端工作区的实现核对过）：- `configured=false` = **还没配置可用账号** ⇒ 页面显示 `error` 里的中文原因（⛔ 不显示「暂无数据」）；- `configured=true` 且 `error` 非空 = 上游查询失败 / 金额解析不了 ⇒ 显示这句中文原因；- 两个余额为 `null` 表示**没拿到**，页面显示 `—`（⛔ 不伪造 `0.00`）。后端实现时 ⛔ 不要改名（尤其是 `configured` / `available_balance` / `frozen_balance`）：页面按这几个键取数，改名会表现为「面板一直空白」而没有任何报错。 */
export type PlatformIntegrationSummary = {
  /** 平台 id（本版只有代练通 = 10）。 */
  platform: number
  /** 平台中文名（后端 `PlatformIntegration.Platform(…).label`，页面直接显示）。 */
  platform_label: string
  /** 平台发单账号（没配置 = 空串）。 */
  account: string
  /** 有没有「启用且默认」的代练通账号（`false` = 无配置，`error` 给下一步）。 */
  configured: boolean
  /** 可用余额（两位小数字符串；没拿到 = `null`）。 */
  available_balance: string | null
  /** 冻结余额（两位小数字符串；没拿到 = `null`）。 */
  frozen_balance: string | null
  /** 中文原因；空串 = 正常。 */
  error: string
}

/** 取右面板摘要（`GET …/platform-integrations/summary/`）。⚠️ 后端可能用 **`code=1002` + 中文 `msg`** 表达「没有可用的代练通账号」/「上游查询失败」（与发单前置闸同一出口）⇒ 调用方必须同时处理「成功载荷里的 `error`」与「请求被拒」两条路，⛔ 不能只写一条。页面侧的分类唯一出处是 `order-pushjdp.ts::summaryErrorReason()`。 */
export async function fetchPlatformIntegrationSummary(): Promise<PlatformIntegrationSummary> {
  return request<PlatformIntegrationSummary>({
    method: 'get',
    url: PLATFORM_INTEGRATION_SUMMARY_URL,
  })
}
