/**
 * 大厅卡片的**显示与抢单分流纯函数**（手机端 `/m/hall`，spec §5.3）。
 * 抽成导出纯函数是承重的（照桌面 `order-insiderecorderlist.ts::claimTargetOf`）：渲染期与点击期各写一份判据必然漂移——表现为「画了按钮、点了静默没反应」或请求打到 `/upstream/null/claim/`。
 */
import type { HallClaimSource, HallListItem } from '@/api/hall'
import { isOrderNoShape } from './orderNoShape'

export function isHallClaimLink(source: unknown, ref: unknown): source is HallClaimSource {
  if (typeof ref !== 'string') return false
  if (source === 'local') return isOrderNoShape(ref)
  return (source === 'upstream' || source === 'peer') && /^[1-9][0-9]{0,14}$/.test(ref)
}

/** 区服显示的**结构化输入**（只取用到的键，单测不必构造整行）。 */
export type ServerDisplayRow = Pick<HallListItem, 'origin' | 'server' | 'remote_game_server'>

/**
 * 区服两源合并显示（spec §5.3）：原生行 → `server` 经区服字典映射；镜像行 → 行内远端文本 `remote_game_server`
 * （它的 `server` 是 null，⛔ 不要拿 null 查我方字典，照桌面 `gameInfo` 按 origin 分支）；两源都拿不到 → 「—」。
 * ⚠️ 判「有没有名字」用去空白后的空串，不是真值（E-0021 同族：空白串不算有名字）。
 */
export function hallServerDisplay(
  row: ServerDisplayRow,
  resolveServerName: (id: number | null) => string,
): string {
  if (row.origin !== 'native') {
    const remote = (row.remote_game_server ?? '').trim()
    return remote === '' ? '—' : remote
  }
  const name = resolveServerName(row.server ?? null).trim()
  return name === '' ? '—' : name
}

/** 抢单分流的**结构化输入**。⚠️ 两个主键都刻意收成 `unknown`：合同上是 `number | null`
 * （`api/hall.ts`），契约漂移（串 / 0 / 负数 / 小数）由本函数收窄，⛔ 不赌上游。 */
export type ClaimSourceRow = Pick<HallListItem, 'origin' | 'order_no'> & {
  mirror_pk: unknown
  sim_pk?: unknown
}

/**
 * 分流结果（D-0296 三路 + ⭐ D-0303 团团通上家第四路）：`upstream`/`peer` 都按**镜像行主键**取行、走各自端点
 * （`/hall/upstream/<pk>/claim/` vs `/hall/peer/<pk>/claim/`，⛔ 路径参数是镜像行主键、不是上家单号）；
 * `native` 走 `claimOrder(orderNo)`；`simulated` 走 `claimSimulatedHallOrder(simPk)`（⚠️ 端点永远回 1012——接不走是产品语义）。
 */
export type ClaimTarget =
  | { kind: 'upstream'; mirrorPk: number }
  | { kind: 'native'; orderNo: string }
  | { kind: 'simulated'; simPk: number }
  | { kind: 'peer'; mirrorPk: number }

/**
 * 本线路的镜像行主键是否是**能用的正整数**（桌面 `isClaimableMirrorPk` 逐字移植）。判据是「正整数」不是真值（E-0021 同族）：
 * `null` / `0` / `NaN` / 负数 / 非整数都不合法，⛔ 不许拿它们拼 URL 发注定打不中的请求。upstream 与 peer 两条镜像线共用这条判据（端点不同）。
 */
export function isClaimableMirrorPk(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
}

/**
 * 这一行接的时候走哪条路（spec §5.3 / D-0296 / ⭐ D-0303）——照桌面 `claimTargetOf` 先分 `origin` 再判主键：
 * simulated/upstream/peer 各自要求主键是**正整数**，否则 ⇒ `null`（fail-closed 数据门：不画按钮、不发请求）。⛔ 镜像行不许落进原生路（没有我方单号、请求注定打不中），peer 也⛔ 不许与 upstream 合并（两张镜像表主键空间互不相干，合并会把单打到另一条端点 / 另一行）；原生行非空 `order_no` ⇒ 原生路，空 ⇒ `null`。渲染期与点击期共用本函数，判据必然一致。
 * 灰卡（`is_exited`）不在本函数管——它是状态门（页面另行挡），这里只回答「接的话走哪条路」。
 */
export function claimTargetOf(row: ClaimSourceRow): ClaimTarget | null {
  if (row.origin === 'simulated') {
    // 模拟单与镜像行共用「正整数主键」这一条判据（`isClaimableMirrorPk` 的谓词本身）。
    return isClaimableMirrorPk(row.sim_pk)
      ? { kind: 'simulated', simPk: row.sim_pk }
      : null
  }
  if (row.origin === 'upstream') {
    return isClaimableMirrorPk(row.mirror_pk)
      ? { kind: 'upstream', mirrorPk: row.mirror_pk }
      : null
  }
  if (row.origin === 'peer') {
    return isClaimableMirrorPk(row.mirror_pk)
      ? { kind: 'peer', mirrorPk: row.mirror_pk }
      : null
  }
  const orderNo = typeof row.order_no === 'string' ? row.order_no : ''
  return orderNo === '' ? null : { kind: 'native', orderNo }
}

/**
 * 行的**业务标识**（卡片 `:key` 与抢单成功后移除卡片用；桌面 `rowKey` 同口径，加前缀防两源键值相撞）。
 * ⚠️ peer 与 upstream 同用 `remote_order_id` 字段但**两张表的单号空间互不相干**（同一单号可能来自两个不同上家）⇒ peer 按 `mirror_pk` 才唯一；主键缺失（契约漂移）时回落 `remote_order_id`，⛔ 不抛异常（页面渲染容错）。
 */
export function hallRowKey(
  row: Pick<HallListItem, 'origin' | 'order_no' | 'remote_order_id'> & {
    sim_pk?: number | null
    mirror_pk?: unknown
  },
): string {
  if (row.origin === 'native') return `native:${row.order_no}`
  if (row.origin === 'simulated') return `sim:${String(row.sim_pk ?? '')}`
  if (row.origin === 'peer') {
    const pk = isClaimableMirrorPk(row.mirror_pk) ? String(row.mirror_pk) : ''
    return `peer:${pk === '' ? row.remote_order_id : pk}`
  }
  return `upstream:${row.remote_order_id}`
}

/** 卡片「订单号」那一行的**结构化输入**（只取用到的键，单测不必构造整行）。 */
export type OrderNoSourceRow = Pick<
  HallListItem,
  'origin' | 'order_no' | 'source_order_no' | 'remote_order_view' | 'remote_order_id'
>

/** 订单号那一行的显示结果：`label` 是小标签，`text` 是**完整**单号（⛔ 页面不截断）。 */
export type HallOrderNoDisplay = { label: string; text: string }

/**
 * 卡片上那一行订单号：原生/模拟行 → 展示单号 = `source_order_no || order_no`（⭐ D-0301：来源单号非空取它、空回退内置号，标签照旧「订单号」；模拟行不标「模拟」——演示卡本意就是让打手当普通单看，D-0296）；
 * 镜像行（upstream/peer）→ `order_no` 恒是空串（不是本方订单）⇒ 回落 `remote_order_view || remote_order_id`（上家单号），⛔ 不能显示空白、也不能把上家单号冒充成我方单号；两源都空（数据异常）→ `text` 空串，页面据此不画这一行。
 * ⚠️ 回落顺序承重：`remote_order_view` 才是打手在上家后台搜到的号，`remote_order_id` 只是兜底（桌面同一条口径）。
 */
export function hallOrderNoDisplay(row: OrderNoSourceRow): HallOrderNoDisplay {
  if (row.origin === 'native' || row.origin === 'simulated') {
    const orderNo = typeof row.order_no === 'string' ? row.order_no : ''
    const sourceNo = typeof row.source_order_no === 'string' ? row.source_order_no : ''
    // 展示值去空白后判空（与镜像行的 `trim` 判据同族）：空白串不算「有来源号」。
    const view = sourceNo.trim()
    return { label: '订单号', text: view !== '' ? view : orderNo.trim() }
  }
  const view = (row.remote_order_view ?? '').trim()
  const text = view !== '' ? view : (row.remote_order_id ?? '').trim()
  return { label: '订单号', text }
}
