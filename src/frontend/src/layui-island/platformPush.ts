/**
 * 代练通平台推送的**显示层口径**（T11a）。
 *
 * 两个消费者：
 *  - `pages/order-orderlist.ts`：订单号列那颗绿色「通」徽标（`platform_push` 摘要）；
 *  - `pages/order-coreordercenter.ts`：第三方平台订单管理页的「交易状态」列 / 徽标。
 *
 * ## 为什么抽成独立模块
 *
 * 状态码 → 中文只该有**一份**实现（`AGENTS.md` §5「不重复同一事实」）。两页各抄一张表，
 * 下次码表变化就会一半新一半旧，而症状是「同一张单在两页显示不同状态」。
 *
 * ## 状态**色档**也只在这一个文件里（P2）
 *
 * 两张表都是逐条抄对标证据件的 switch，页面里 ⛔ 不写死任何状态色：
 *  - `REMOTE_STATUS_BADGE_CLASSES`：平台**交易状态** → 填充标签底色（`pltmOrderStaRow`）；
 *  - `ORDER_STATUS_TEXT_COLORS`：**系统状态** → 文字颜色（`orderStaRow`）。
 *
 * ## 中文的出处（⛔ 别按任务书里的缩写改）
 *
 * 逐字取 `docs/DAILIANTONG-API.md` §5 与 `apps/business/dailiantong_actions.py::REMOTE_STATUS_LABELS`：
 * `14 = 订单异常`（**不是**「异常」——页面页签叫「异常中」，那是**分类名**，与状态显示值不同）。
 * `10 = 临时订单` 也照码表保留：本仓今天不生产它，但返回值域必须与文档一致。
 *
 * ⭐ 2026-10-28 真机（单 `41017966281869299594`）：`18 = 已下架`。上游码表（§5）只有 10–17，
 * 但**下架后上游就是回 18**（我方轮询同步正常、`remote_status` 确实写成了 18），而旧码表只到
 * 17 ⇒ 界面把真实状态显示成「未知（18）」。⛔ **不许只改后端**：这张表与
 * `apps/business/dailiantong_actions.py::REMOTE_STATUS_LABELS` 必须同值（两边各有一条测试钉字面值）。
 *
 * ## 没有数据时不编
 *
 * - `remote_status = null`（还没回读过）⇒ `remoteStatusLabel()` 回 `null`，调用方画「—」；
 * - `state = closed`（已在平台删除）⇒ 显示「已关闭」（业务方 2026-10-27 口径）；
 * - 其余本地状态（pending / sent / failed / unknown）只在**徽标悬停**里当兜底，且中文逐字取
 *   `DailiantongPush.State` 的 choices——那是真数据，不是编的「未知」。
 */
import type { PlatformPushSummary } from '@/api/dailiantongPush'
import { AuthExpiredError, BizError, HttpError, NetworkError } from '@/api/errors'
import { escapeHtml } from '@/layui-island/escape'

/**
 * 上游 `Status` 码 → 中文（`docs/DAILIANTONG-API.md` §5）。
 *
 * ⚠️ `Record<number, string>` 而不是枚举：后端列是 `IntegerField`，未知新码必须能安全落空
 * （见 `remoteStatusLabel` 的 `null` 回落），⛔ 不要用 `as` 硬转成已知键。
 */
export const REMOTE_STATUS_LABELS: Readonly<Record<number, string>> = {
  10: '临时订单',
  11: '未接手',
  12: '正在代练',
  13: '等待验收',
  14: '订单异常',
  // ⭐ 真机口径：`Status=15` = **被我们锁定**（`LevelOrderLock(Locked=1)` 在 12/13/14
  // 上执行后回 15；`Locked=0` 解锁又回到锁定前的 12/13/14）⇒ 中文「已锁定」，
  // ⛔ 不是「帐号被平台锁」。与后端 `dailiantong_actions.REMOTE_STATUS_LABELS` 同值。
  15: '已锁定',
  16: '撤销中',
  17: '已结算',
  // ⭐ 真机实测：下架后上游回 `Status=18`（上游码表没列这一档）⇒「已下架」。
  // ⛔ 与后端 `dailiantong_actions.REMOTE_STATUS_LABELS` 同值，两边各有测试钉住字面值。
  18: '已下架',
}

/**
 * 上游 `CancelStatus` 码 → 中文（`docs/DAILIANTONG-API.md` §5 的右半张表）。
 *
 * 与 `REMOTE_STATUS_LABELS` 同一口径：`Record<number, string>` + 未知码落空
 * （⛔ 不要 `as` 硬转；后端列是 `IntegerField`，新码必须能安全落空）。
 */
export const REMOTE_CANCEL_STATUS_LABELS: Readonly<Record<number, string>> = {
  10: '未撤销',
  11: '协商撤销中',
  12: '协商已处理',
  13: '撤销已取消',
  14: '客服介入中',
  15: '客服已处理',
  16: '客服强制撤销',
}

/** 本地推送状态的中文（后端 `DailiantongPush.State` 的 choices；徽标悬停与平台单号 tooltip 共用）。 */
const PUSH_STATE_LABELS: Readonly<Record<string, string>> = {
  pending: '发送中',
  sent: '已发出',
  failed: '发送失败',
  unknown: '状态未知',
  closed: '已关闭',
}

/**
 * 本地推送状态 → 中文；未知值原样返回。
 *
 * 与 `config/orderStatus.ts::orderStatusLabel` 同口径：显示一个生枚举值比显示空白更容易
 * 看出契约漂移，⛔ 不把它吞成「未知」。
 */
export function platformPushStateLabel(state: string): string {
  return PUSH_STATE_LABELS[state] ?? state
}

/**
 * `remote_status` → 中文；`null` / 未知码 ⇒ `null`（调用方画「—」）。
 *
 * ⚠️ 用 `is not None` 判空（`AGENTS.md` §5-E）：`0` 不在码表里，但别用真值判据赌将来不会有。
 */
export function remoteStatusLabel(status: number | null | undefined): string | null {
  if (status === null || status === undefined) return null
  return REMOTE_STATUS_LABELS[status] ?? null
}

/**
 * `remote_cancel_status` → 中文；`null` / 未知码 ⇒ `null`（调用方画「—」）。
 *
 * 同 `remoteStatusLabel` 的判空口径（`is not None`，`AGENTS.md` §5-E）。
 */
export function remoteCancelStatusLabel(status: number | null | undefined): string | null {
  if (status === null || status === undefined) return null
  return REMOTE_CANCEL_STATUS_LABELS[status] ?? null
}

/* ── ⭐ h1（2026-10-28 真机 bug）：「交易状态」的**综合状态**（`Status` + `CancelStatus`）──
 *
 * 真机现场（单 `20261027213008801477`）：远端 `Status=16`（撤销中）+ `CancelStatus=12`
 * （协商已处理 = 打手已同意撤销），本地订单已经是「已退单」，页面却一直显示「撤销中」
 * ——因为这一格只看 `remote_status`。
 *
 * 口径（业务方 2026-10-28，与后端 `dailiantong_push.display_status_for_remote` 同一张表）：
 *
 * | 远端 | token / 中文 |
 * |---|---|
 * | `CancelStatus=12` 协商已处理 | `revoked` / 已撤销 |
 * | `CancelStatus=15` 客服已处理 | `revoked` / 已撤销 |
 * | `CancelStatus=16` 客服强制撤销 | `force_revoked` / 已强制撤销 |
 * | `CancelStatus=11` 协商撤销中 | `cancelling` / 撤销中 |
 * | `CancelStatus=14` 客服介入中 | `arbitrating` / 客服介入中 |
 * | `CancelStatus=13` 撤销已取消 | **按 `Status`**（回到正常流程） |
 * | 其余 | 按 `Status`（10–18 各一档） |
 *
 * **优先用后端下发的 `display_status_label`**（后端能拿到读面当次的事实、也会跟着扩展新码）；
 * 只有在旧回包缺这两个键时才走本文件这份等价推导（⛔ 不许退回「只看 `remote_status`」）。
 */

/** 综合状态 token → 中文（与后端 `DISPLAY_STATUS_LABELS` 逐字同源）。 */
export const DISPLAY_STATUS_LABELS: Readonly<Record<string, string>> = {
  temp_order: '临时订单',
  unaccepted: '未接手',
  in_progress: '正在代练',
  waiting_review: '等待验收',
  abnormal: '订单异常',
  // ⭐ 真机口径：15 = 被我们锁定（解锁会回到 12/13/14）⇒「已锁定」。
  locked: '已锁定',
  cancelling: '撤销中',
  settled: '已结算',
  revoked: '已撤销',
  force_revoked: '已强制撤销',
  arbitrating: '客服介入中',
  // ⭐ 真机实测：`Status=18` = 已下架（上游码表没列这一档）。
  off_shelf: '已下架',
}

/** 远端 `Status` → 综合状态 token（`CancelStatus` 没给出可判语义时才走这里）。 */
export const DISPLAY_TOKEN_BY_REMOTE_STATUS: Readonly<Record<number, string>> = {
  10: 'temp_order',
  11: 'unaccepted',
  12: 'in_progress',
  13: 'waiting_review',
  14: 'abnormal',
  15: 'locked',
  16: 'cancelling',
  17: 'settled',
  // ⭐ 真机实测：18 = 已下架（上游码表只有 10–17）。
  18: 'off_shelf',
}

/** 综合状态 → 填充标签底色（色值仍只在这一个文件里出现）。 */
export const DISPLAY_STATUS_BADGE_CLASSES: Readonly<Record<string, string>> = {
  temp_order: 'layui-badge layui-bg-gray',
  unaccepted: 'layui-badge layui-bg-gray',
  in_progress: 'layui-badge layui-bg-green',
  waiting_review: 'layui-badge layui-bg-blue',
  abnormal: 'layui-badge',
  locked: 'layui-badge',
  cancelling: 'layui-badge layui-bg-orange',
  settled: 'layui-badge layui-bg-black',
  // 已撤销 / 已强制撤销 / 客服介入中 —— 沿用对标 `pltmOrderStaRow` 已有的那几档：
  // 70 已撤销 = 灰（⛔ 不新造颜色）、55 仲裁中 = 橙。
  revoked: 'layui-badge layui-bg-gray',
  force_revoked: 'layui-badge layui-bg-gray',
  arbitrating: 'layui-badge layui-bg-orange',
  // ⭐ 18 已下架：**推断**档 —— 对标 `pltmOrderStaRow` switch 里没有「已下架」这一档
  // （它那张表的 70 是「已撤销」）。语义上「已下架」= 这张单不在可接列表里，最接近的既有档
  // 是「未接手（对标 10）/ default」的灰 ⇒ 取灰，⛔ 不新造一个颜色（`AGENTS.md` §5-D）。
  off_shelf: 'layui-badge layui-bg-gray',
}

/**
 * 「交易状态」这一格需要的最小字段集。
 *
 * ⚠️ 三个读面（订单列表摘要 / 第三方订单列表行 / 详情推送行）都满足它；
 * `display_status*` 写成可选是因为**旧回包可能没有**（后端 h1 之前的下发）。
 */
export type PlatformStatusFields = {
  state: PlatformPushSummary['state']
  remote_status: number | null
  remote_cancel_status?: number | null
  display_status?: string | null
  display_status_label?: string | null
}

/** token → 中文；未知 token 原样返回（显示生枚举值比空白更容易看出契约漂移）。 */
function displayStatusLabelOf(token: string): string {
  return DISPLAY_STATUS_LABELS[token] ?? token
}

/** 本地按码推导综合状态（后端没下发 `display_status` 时用；与后端同一张表）。 */
function derivedDisplayStatus(
  push: PlatformStatusFields,
): { token: string; label: string } | null {
  const status = push.remote_status
  const cancel = push.remote_cancel_status
  // ⚠️ 与后端同一优先级：`Status=17`（钱已结算）先判，否则页面会说「已撤销」而本地已完成。
  if (status === 17) return { token: 'settled', label: displayStatusLabelOf('settled') }
  if (cancel === 12 || cancel === 15) {
    return { token: 'revoked', label: displayStatusLabelOf('revoked') }
  }
  if (cancel === 16) {
    return { token: 'force_revoked', label: displayStatusLabelOf('force_revoked') }
  }
  if (cancel === 11) {
    return { token: 'cancelling', label: displayStatusLabelOf('cancelling') }
  }
  if (cancel === 14) {
    return { token: 'arbitrating', label: displayStatusLabelOf('arbitrating') }
  }
  // 13 撤销已取消 / 10 未撤销 / 未知码 ⇒ 回到按 `Status` 显示。
  const token = status === null || status === undefined
    ? undefined
    : DISPLAY_TOKEN_BY_REMOTE_STATUS[status]
  if (token === undefined) return null
  return { token, label: displayStatusLabelOf(token) }
}

/**
 * 这一行的**综合状态**：`{token, label}`；没有任何可判状态 ⇒ `null`（调用方画「—」）。
 *
 * 优先级：`state=closed`（已在平台删除 ⇒ 已关闭）→ 后端 `display_status` /
 * `display_status_label` → 本地按 `(Status, CancelStatus)` 推导。
 */
export function platformDisplayStatus(
  push: PlatformStatusFields,
): { token: string; label: string } | null {
  if (push.state === 'closed') return { token: 'closed', label: '已关闭' }
  const backendToken =
    typeof push.display_status === 'string' ? push.display_status.trim() : ''
  if (backendToken !== '') {
    const backendLabel =
      typeof push.display_status_label === 'string'
        ? push.display_status_label.trim()
        : ''
    return {
      token: backendToken,
      label: backendLabel !== ''
        ? backendLabel
        : displayStatusLabelOf(backendToken),
    }
  }
  return derivedDisplayStatus(push)
}

/** 没有可判定状态（没回读 / 未知码 / 已在平台删除）时的灰档（对标 switch 的 default）。 */
export const STATUS_BADGE_FALLBACK_CLASS = 'layui-badge layui-bg-gray'

/**
 * 交易状态标签的**底色档**（P2）：`remote_status` → 对标同款 `layui-badge` class。
 *
 * ⛔ **颜色不凭记忆猜**：唯一出处是对标 `CoreOrderCenter.js:427-462` 的 `pltmOrderStaRow`
 * switch（它按对标自己的状态码换 class）；那些码的中文名在证据件页签的 `lay-id` 上
 * （`order-coreordercenter.html:172-187`）：
 *
 * | 对标码 | 中文 | 对标 class | 我方 `remote_status` |
 * |---|---|---|---|
 * | 10 | 未接手 | `layui-badge layui-bg-gray` | 11（未接手） |
 * | 20 | 代练中 | `layui-badge layui-bg-green` | 12（正在代练） |
 * | 25 | 已锁定 | `layui-badge`（基色红） | 15（已锁定，⭐ 真机改准：被我们锁定） |
 * | 30 | 异常中 | `layui-badge`（基色红） | 14（订单异常） |
 * | 40 | 待验收 | `layui-badge layui-bg-blue` | 13（等待验收） |
 * | 50 | 撤销中 | `layui-badge layui-bg-orange` | 16（撤销中） |
 * | 55 / 56 | 仲裁中 / 已仲裁 | 橙 / 青 | 客服介入中（橙，见 `DISPLAY_STATUS_BADGE_CLASSES`） |
 * | 60 | 已结算 | `layui-badge layui-bg-black` | 17（已结算） |
 * | 70 / default | 已撤销 / 其它 | `layui-badge layui-bg-gray` | 已撤销（12/15）、未知码 |
 *
 * 「填充块 + 白字 + 圆角」由 layui 自带 `.layui-badge` 提供（`layui.css`：
 * `padding:0 6px; background-color:#ff5722; color:#fff; border-radius:2px`；
 * 各 `layui-bg-*` 只换 background-color）。⚠️ `.layui-bg-gray` 会把字色改成深灰
 * （`#5f5f5f`）——那是**对标用同一个 class 的实际渲染**，⛔ 不为了「白字」另造一个灰。
 *
 * ⭐ h1（2026-10-28 真机）：`remote_status` 本身**没有**独立的「已撤销」——远端撤销完成后
 * `remote_status` 仍是 16（撤销中），只有 `remote_cancel_status` 变成 12/15/16。
 * ⇒ 这一格必须按**综合状态**取色（见 `platformDisplayStatus` / `DISPLAY_STATUS_BADGE_CLASSES`），
 * 本表只剩「`CancelStatus` 没给出可判语义时按 `Status` 取色」这一级兜底。
 * 两处色值仍只在本文件出现：本表**由综合状态表推导**，⛔ 不另抄一份 class 字面量。
 */
export const REMOTE_STATUS_BADGE_CLASSES: Readonly<Record<number, string>> =
  Object.fromEntries(
    Object.entries(DISPLAY_TOKEN_BY_REMOTE_STATUS).map(([code, token]) => [
      Number(code),
      DISPLAY_STATUS_BADGE_CLASSES[token] ?? STATUS_BADGE_FALLBACK_CLASS,
    ]),
  ) as Readonly<Record<number, string>>

/**
 * 交易状态标签的 class；参数形状与 `platformPushStatusLabel` 一致（两处口径必须同源）。
 *
 * - `state=closed` ⇒ 灰（「已关闭」，业务方 2026-10-27 口径）；
 * - 有综合状态（后端 `display_status`，或本地按 `(Status, CancelStatus)` 推导）
 *   ⇒ 查 `DISPLAY_STATUS_BADGE_CLASSES`（已撤销 = 对标 70 那一档灰）；
 * - **没有可判的远端状态**时：本地 `state=failed` ⇒ 红、`unknown` ⇒ 橙
 *   （⭐ 评审 2026-10-28：跟列表页 `PUSH_BADGE_CLASSES` 的徽标对齐——同一条推送在两个页面
 *   不能一个红一个灰；⛔ 只对齐这两档，`sent` / `pending` 仍走 default 灰）；
 * - 其余 ⇒ default 灰（对标 switch 的 `default` 档）。
 */
export function platformStatusBadgeClass(push: PlatformStatusFields): string {
  if (push.state === 'closed') return STATUS_BADGE_FALLBACK_CLASS
  const display = platformDisplayStatus(push)
  if (display !== null) {
    return DISPLAY_STATUS_BADGE_CLASSES[display.token] ?? STATUS_BADGE_FALLBACK_CLASS
  }
  if (push.state === 'failed') return `layui-badge ${PUSH_BADGE_CLASSES.failed}`
  if (push.state === 'unknown') return `layui-badge ${PUSH_BADGE_CLASSES.unknown}`
  return STATUS_BADGE_FALLBACK_CLASS
}


/* ── 「系统状态」列的文字颜色（P2 补充，dl-E 对拍 2026-10-27 抓到丢了） ──────── */

/**
 * 我方 `OrderStatus` → 对标 `orderStaRow`（`CoreOrderCenter.js:330-365`）的文字色。
 *
 * ⛔ **色值逐条抄那张 switch，不猜**。两边的档位名字不是一一同义（对标那边是它自己的业务状态），
 * 对应关系按**中文同名**取，唯一一个对不上的是 `UNASSIGNED`（未分配）——对标没有这一档
 * ⇒ 用它 switch 的 `default` 值（`#b0aeae`，见 `ORDER_STATUS_TEXT_FALLBACK_COLOR`），不编新色。
 *
 * ⚠️ 这张表与 `REMOTE_STATUS_BADGE_CLASSES`（平台交易状态的**填充标签**底色）同住本文件：
 * 两处色档是「状态显示的唯二出处」，改色只改这里，⛔ 不要在页面里写死颜色。
 *
 * | 我方状态 | 中文 | 对标档 | 色值 |
 * |---|---|---|---|
 * | `WAITING_GRAB` | 待抢单 | 待抢单 | `#D15FE8` |
 * | `IN_PROGRESS` | 代练中 | 代练中 | `#219227` |
 * | `PENDING_REVIEW` | 待验收 | 待验收 | `#ff6a00` |
 * | `COMPLETED` | 已完成 | 已完成 | `#0094ff` |
 * | `ISSUE` | 问题单 | 问题单 | `#ff0000` |
 * | `PAUSED` | 已暂停 | 已暂停 | `#FF5722` |
 * | `REFUNDED` | 已退单 | 已退单 | `#efaaaa` |
 * | `UNASSIGNED` | 未分配 | **无对应档** | `#b0aeae`（对标 switch 的 default） |
 */
export const ORDER_STATUS_TEXT_COLORS: Readonly<Record<string, string>> = {
  WAITING_GRAB: '#D15FE8',
  IN_PROGRESS: '#219227',
  PENDING_REVIEW: '#ff6a00',
  COMPLETED: '#0094ff',
  ISSUE: '#ff0000',
  PAUSED: '#FF5722',
  REFUNDED: '#efaaaa',
}

/**
 * 「系统状态」文字色的**回落值** = 对标 `orderStaRow` 的 `default`（`#b0aeae`）。
 *
 * 三种情况都用它：我方码表里没有这一档（`UNASSIGNED`）、后端回了前端还不认识的新状态
 * （枚举漂移）、以及页面没有状态可显示之外的兜底。⛔ 不为了「好看」另挑一个颜色。
 */
export const ORDER_STATUS_TEXT_FALLBACK_COLOR = '#b0aeae'

/**
 * 系统状态 → 文字颜色；`''`（后端没给状态）⇒ **空串**（调用方画「—」，⛔ 不给「没有数据」上色）。
 *
 * 未知取值 ⇒ 对标 default 色（显示一个生枚举值比空白更容易看出契约漂移，同 `orderStatusTagType`）。
 */
export function orderStatusTextColor(status: string): string {
  if (status === '') return ''
  return ORDER_STATUS_TEXT_COLORS[status] ?? ORDER_STATUS_TEXT_FALLBACK_COLOR
}

/**
 * 徽标悬停 / 页签里的「当前状态」文案 = **综合状态**的中文。
 *
 * 优先级：`state=closed` ⇒「已关闭」（业务方口径）→ 后端 `display_status_label` →
 * 本地按 `(Status, CancelStatus)` 推导 → 本地 `state` 中文（真数据兜底）→「—」。
 *
 * ⭐ h1（2026-10-28 真机 bug）：**必须一起看 `remote_cancel_status`** ——
 * `Status=16`（撤销中）+ `CancelStatus=12`（协商已处理）那一单的真实状态是**已撤销**，
 * ⛔ 只读 `remote_status` 会一直显示「撤销中」。
 *
 * ⚠️ 参数放宽成 `PlatformStatusFields`：订单详情「代练通」页签拿到的是**完整推送行**
 * （`DailiantongPushRow`，比摘要多价格 / 时间 / `last_error`），它同样有这几个键
 * ⇒ 直接复用这里的优先级，⛔ 别在页面里再抄一遍「已关闭 → 远端 → 本地」。
 */
export function platformPushStatusLabel(push: PlatformStatusFields): string {
  if (push.state === 'closed') return '已关闭'
  const display = platformDisplayStatus(push)
  if (display !== null) return display.label
  const local = platformPushStateLabel(push.state)
  return local === '' ? '—' : local
}

/**
 * 徽标底色：按**本地推送状态**区分，⛔ 不再一律绿色（状态不同却长得一样 = 界面在说假话）。
 *
 * | state | 底色 | 含义 |
 * |---|---|---|
 * | `sent` / `pending` | 绿 `layui-bg-green` | 已发出 / 发送中，正常 |
 * | `unknown` | 橙 `layui-bg-orange` | 结果未知，**待核对** |
 * | `failed` | 红 `layui-bg-red` | 发送失败 |
 * | `closed` | 灰 `layui-bg-gray` | 已在平台删除 / 关闭 |
 *
 * 未知取值按橙处理（宁可提示核对，也不冒充正常）。
 */
export const PUSH_BADGE_CLASSES: Readonly<Record<string, string>> = {
  pending: 'layui-bg-green',
  sent: 'layui-bg-green',
  unknown: 'layui-bg-orange',
  failed: 'layui-bg-red',
  closed: 'layui-bg-gray',
}

/** 本地推送状态 → 徽标底色；未知状态按橙（待核对）。 */
export function platformPushBadgeClass(state: string): string {
  return PUSH_BADGE_CLASSES[state] ?? 'layui-bg-orange'
}

/**
 * 订单号列那颗「通」徽标；`null` / 缺键 ⇒ **空串**（⛔ 不占位、不画「-」）。
 *
 * 形状：layui 的 `layui-badge` + **按状态取的底色**（`platformPushBadgeClass`）。悬停文案
 * 由 `title` 承载（vendored layui 里 `lay-tips` 是死属性，只有 layuiadmin 认它；本页已有同族先例）。
 *
 * ⚠️ `serial_no` 是上游回包值（字符串）⇒ **进属性前过 `escapeHtml`**。
 * 没有单号时显示「—」（那是「这一格没有数据」的既有约定），⛔ 不拼一个假单号。
 */
export function platformPushBadgeHtml(
  push: PlatformPushSummary | null | undefined,
): string {
  if (push === null || push === undefined) return ''
  const serial = push.serial_no === '' ? '—' : push.serial_no
  const title = `代练通 · 平台单号 ${serial} · 状态 ${platformPushStatusLabel(push)}`
  const cls = platformPushBadgeClass(push.state)
  return `<span class="layui-badge ${cls}" title="${escapeHtml(title)}">通</span>`
}


/* ───────────── 写动作失败的 failed / unknown 分档（两页共用） ───────────── */

/** 写动作失败的两档：`failed` = 后端明确说没成；`unknown` = 不知道成没成。 */
export type PushErrorKind = 'failed' | 'unknown'

/**
 * 给**写动作**的错误分档（⛔ 不许把「不知道」说成「失败」）。
 *
 * | 错误 | 分档 | 理由 |
 * |---|---|---|
 * | `BizError`（后端业务码，含前置闸 1002 / 参数 1000） | `failed` | 后端明确拒绝了，没发出去 |
 * | `HttpError` 4xx（400 / 401 / 403 / 404 …） | `failed` | 请求被后端挡下；`msg` 是后端原文 |
 * | `HttpError` 5xx | `unknown` | 可能已经写进上游、只是回包坏了 |
 * | `NetworkError`（断网 / 超时 / 非信封 / 500 HTML） | `unknown` | 请求可能已到达并执行 |
 * | `AuthExpiredError`（401） | `failed` | 没带有效会话，写动作不会执行 |
 * | 其它未分类 | `unknown` | 宁可让人去核对，也不谎报失败 |
 *
 * ⚠️ 两个消费者：发单页（`order-pushjdp`）与订单详情「代练通」页签（`order-orderview`）。
 * 分档规则只能有这一处实现，否则两页会对同一类失败说两种话。
 */
export function classifyPushError(error: unknown): PushErrorKind {
  if (error instanceof BizError) return 'failed'
  if (error instanceof HttpError) return error.status >= 500 ? 'unknown' : 'failed'
  if (error instanceof AuthExpiredError) return 'failed'
  if (error instanceof NetworkError) return 'unknown'
  return 'unknown'
}

/* ───────── 发单页「发单成功」跨窗口协议（2026-10-28 真机） ───────── */

/**
 * 发单成功后，**发单页 iframe**（`order-pushjdp`）→ **父窗口**（订单列表 / 订单详情）的消息类型。
 *
 * 为什么必须有这条协议：发单页跑在 `layer.open({type:2})` 的 iframe 里，父窗口才是弹层的宿主
 * ⇒ 只有它能「关弹窗 + 刷新列表 + 出提示」。⛔ 子页面**不要**直接摸 `parent.layer.close`
 * （本页也会被单独打开，那种情形下 `window.parent === window`，会摸到一个不存在的 layer）。
 *
 * 契约与 `island:order-saved` / `island:navigate` 同形：**同源校验 + `type` 白名单**。
 * 两端**只有这一份定义**（子页面导出、两个父页面 import）——各写一份字面量会在改名时静默断链。
 */
export const PUSH_JDP_SENT_MESSAGE_TYPE = 'island:push-jdp-sent'

/**
 * 发单成功后由**父窗口**显示的那句提示（弹窗关掉之后才看得见，子页面自己弹的会随 iframe 消失）。
 *
 * 文案由业务方 2026-10-28 真机口径给定（「发单成功」），⛔ 不要改写成「已提交」。
 */
export const PUSH_JDP_SENT_TEXT = '发单成功'
