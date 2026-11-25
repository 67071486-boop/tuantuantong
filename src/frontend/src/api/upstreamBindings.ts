// 「绑定上家系统设置」页那张**统一绑定列表**的出口（计划
// `docs/plans/2026-10-04-upstream-binding-list-rework.md` §2.1；后端已落地）。
//
// | 方法 | 路径 | 用途 |
// |---|---|---|
// | GET | `/api/business/upstream-bindings/` | 团团通上家（`PeerUpstreamBinding`）与鸡蛋铺上家（`JidanpuUpstreamBinding`）**混在一张表**里的只读列表（`{items,total}`，**服务端不分页**） |
//
// 分层与 `api/peerBindings.ts` / `api/jidanpuUpstream.ts` 同形：传输、认证（CSRF）与信封解包全在
// `request()`（`./http.ts`）；本文件**不 import 任何 UI 库**。
//
// ⚠️ **本文件只有读那一半**：写请求**刻意不在这里**——
//   - 新增 / 覆盖写鸡蛋铺那一行仍走 `api/jidanpuUpstream.ts::putUpstreamBinding`（弹窗的鸡蛋铺分支）；
//   - 删除一条鸡蛋铺绑定走 `api/jidanpuUpstream.ts::deleteUpstreamBinding`（计划 §2.3 指定的落点）；
//   - 团团通那条链路的读 / 写 / 删仍走 `api/peerBindings.ts`。
//   本文件的存在只为把**那张表的契约（URL + 13 键行形状）**放在与后端同一层的地方，
//   ⛔ 不是新建一条写路径。
//
// ⚠️ **凭据红线**（计划 §1）：行里**没有** `password` / `password_set` / `token_*` / OSS 凭证 / cookie。
//   ⚠️ 本线**有意扩大**一处（计划 §1 的有记录偏离）：鸡蛋铺行的**上家子账号**（`account`，登录名）
//   会出现在列表里——⛔ 不要顺手把它加进「第三方平台 → 鸡蛋铺系统同步」的只读总览端点。

/** 统一列表一行的**恰好 13 个键**（= 后端 `apps/business/api/upstream_binding_views.py` 的 `ROW_KEYS`；2026-10-25 大厅镜像 P1 起多两格）。⚠️ 这是后端契约的**前端副本**：⛔ 多一个 / 少一个 / 改名都会让页面读到 `undefined`（而页测试若拿夹具自造的键去断言就会**说谎**）⇒ 两处同改，后端那份才是权威。⚠️ `consecutive_failures` 对**鸡蛋铺行恒 `0`**：那张模型没有这一列（计划 §2.1 / §1 红线），不是「暂时填 0」。R20 那块失败红字的判据 `> 0` 因此天然不会把鸡蛋铺行算成失败。 */
export type UpstreamBindingRow = {
  /** 两类行各自的主键（团团通 = `PeerUpstreamBinding.pk`、鸡蛋铺 = `JidanpuUpstreamBinding.pk`）。 */
  id: number
  /** 系统类型：`tuantuantong` / `jidanpu`（计划 §2.1 的字面值）。
   * ⛔ **不要直接上屏**（用户看不懂机器值）：列表那一列经 `systemTypeLabel` 换成
   * `团团通系统` / `鸡蛋铺系统`（计划 §2.3）。 */
  system_type: string
  /** 上家系统名称：团团通 = `peer_name`；鸡蛋铺 = `name`（人填，⭐ ）优先、
   * 回落 `account_name`（同步引擎缓存的账号显示名）。
   * ⚠️ 鸡蛋铺那一份两个都空时是**空串** ⇒ 页面按计划 §2.3 回 `—`，⛔ 不用地址编一个名字。 */
  system_name: string
  /** 上家站点地址（团团通 = `peer_base_url`；鸡蛋铺 = `jidanpu_base_url`，尾斜杠后端已归一化）。 */
  base_url: string
  /** 上家子账号：团团通 = `peer_account`；鸡蛋铺 = `account`（登录名）。
   * ⚠️ 本线**有意**把它摆上屏（计划 §1 的有记录扩大）。 */
  account: string
  /** 绑定操作人的**显示名**（团团通 = 序列化器的 `bound_by_name`；鸡蛋铺 = `user_display_name`）。 */
  bound_by_name: string
  /** 绑定时刻（团团通 = `bound_at`；鸡蛋铺 = `created_at`）。列表按它**降序**。 */
  bound_at: string
  /** 上一次**成功**收到同步的时刻；从没成功过是 `null`（A19 那块信息读它）。 */
  last_ok_at: string | null
  /** 最近的失败原因（人话）；没有失败时是空串（R20 那块红字读它）。 */
  last_error: string
  /** 最近一次失败的时刻；没有失败时是 `null`。 */
  last_error_at: string | null
  /** 连续失败次数（R20 的判据是 `> 0`）；**鸡蛋铺行恒 `0`**（模型没有这一列）。 */
  consecutive_failures: number
  /** ⭐ 大厅镜像 P1：「同步上家抢单大厅」开关（团团通行 = 真 `boolean`，默认 `false`）。
   * ⚠️ **鸡蛋铺行恒 `null`**：那张模型**没有**这个概念（`null` = 没有这一列；
   * `false` = 有、且关着）⇒ 页面据此**不画**那个开关，⛔ 不要拿 `enabled` 顶替。 */
  hall_sync_enabled: boolean | null
  /** ⭐ 大厅价格百分比（1–100）。
   * ⚠️ 两类行**都有真值**，但含义不同：团团通行 = 大厅展示价的百分比（本线新增）；
   * 鸡蛋铺行 = 它自己的「发单价百分比」（往里接时的勾兑），本页只做展示 / 回填。 */
  hall_price_percent: number
}

/** 初始为 null（使用绑定默认规则），非空列表按游戏限同步，单项 null 沿用绑定默认比例。 */
export type UpstreamGameRule = {
  game_id: number
  hall_price_percent: number | null
}

export type UpstreamGameRules = UpstreamGameRule[] | null

/** 集合端点（尾斜杠是本仓业务集合路径的统一口径）。 */
export const UPSTREAM_BINDINGS_URL = '/api/business/upstream-bindings/'
