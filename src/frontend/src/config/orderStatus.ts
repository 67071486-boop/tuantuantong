/**
 * 订单状态的**静态**镜像：中文 label 与 Element Plus 的语义 tag 类型。
 *
 * 唯一出处是后端 `src/apps/business/models.py` 的 `OrderStatus`（8 个 TextChoices）。
 * 后端改这里必须在同一提交里同步——前端没有自动校验（骨架 spec §12「契约漂移无自动校验」）。
 *
 * ⚠️ **这里没有、也不得有「可达目标白名单」**：那是会漂移的部分，漂移的表现是「少一个选项」，
 * 用户看不到任何错误，只会发现某个操作做不到。改由后端在订单详情里返回 `allowed_transitions`
 * （spec D8）。前端只镜像**静态**的 label 与颜色。
 *
 * ⚠️ **不写任何色值**：用 Element Plus 的语义 tag 类型。主题 spec §2.1 已把
 * `--el-color-primary` 指到 `var(--tt-color-primary)`，语义色跟着主题走；
 * 自己写死 `#16baaa` 之类会在主题计划改动时漂移。
 */
export type OrderStatus =
  | 'UNASSIGNED'
  | 'WAITING_GRAB'
  | 'IN_PROGRESS'
  | 'PENDING_REVIEW'
  | 'COMPLETED'
  | 'ISSUE'
  | 'PAUSED'
  | 'REFUNDED'

/** Element Plus 的 tag 类型（`el-tag` 的 `type` 属性）。 */
export type EpTagType = 'info' | 'warning' | 'primary' | 'success' | 'danger'

/** 声明顺序 = 后端 `OrderStatus` 的声明顺序。 */
export const ORDER_STATUS_VALUES: readonly OrderStatus[] = [
  'UNASSIGNED',
  'WAITING_GRAB',
  'IN_PROGRESS',
  'PENDING_REVIEW',
  'COMPLETED',
  'ISSUE',
  'PAUSED',
  'REFUNDED',
] as const

/** 中文 label **逐字**取自后端 `OrderStatus` 的 TextChoices 第二个元素。
 *
 * ⛔ **D-0344「旧前端一条不改」**：本表被旧 island 复刻页（order-myorderlist 等）与 v2 两边
 * 消费，**改值会改旧前端的显示文案** ⇒ 业务方 2026-11-03「待抢单改待接单」只落在 v2 ——
 * 见下方 `V2_ORDER_STATUS_LABELS`（只覆盖措辞、枚举与顺序不动）。 */
export const ORDER_STATUS_LABELS: Record<OrderStatus, string> = {
  UNASSIGNED: '未分配',
  WAITING_GRAB: '待抢单',
  IN_PROGRESS: '代练中',
  PENDING_REVIEW: '待验收',
  COMPLETED: '已完成',
  ISSUE: '问题单',
  PAUSED: '已暂停',
  REFUNDED: '已退单',
}

export const ORDER_STATUS_TAG_TYPES: Record<OrderStatus, EpTagType> = {
  UNASSIGNED: 'info',
  WAITING_GRAB: 'warning',
  IN_PROGRESS: 'primary',
  PENDING_REVIEW: 'warning',
  COMPLETED: 'success',
  ISSUE: 'danger',
  PAUSED: 'info',
  REFUNDED: 'danger',
}

/**
 * 后端可能加新状态而前端还没同步。这时**原样返回**那个字符串（而不是抛错或显示 `undefined`）——
 * 显示一个生枚举值虽然难看，但比让整页崩掉或显示空白要好，而且它一眼就能看出是漂移。
 */
export function orderStatusLabel(status: string): string {
  return orderStatusLabelOrNull(status) ?? status
}

function orderStatusLabelOrNull(status: string): string | null {
  return isOrderStatus(status) ? ORDER_STATUS_LABELS[status] : null
}

/** 未知取值 → 灰色 tag（EP 的 `info`），同样是「不崩」而不是「猜一个状态」。 */
export function orderStatusTagType(status: string): EpTagType {
  return isOrderStatus(status) ? ORDER_STATUS_TAG_TYPES[status] : 'info'
}

function isOrderStatus(status: string): status is OrderStatus {
  return (ORDER_STATUS_VALUES as readonly string[]).includes(status)
}

/**
 * ⭐ **v2 专属词表覆盖**（2026-11-03 业务方「待抢单 文字改成 待接单」）：只覆盖措辞，
 * 枚举集合与顺序照 `ORDER_STATUS_LABELS`；只许 v2 组件消费（`src/v2/**`），
 * 旧 island 复刻页继续用共享表（D-0344 口径）。状态签/卡片的中文一列走它。
 */
export const V2_ORDER_STATUS_LABELS: Record<OrderStatus, string> = {
  ...ORDER_STATUS_LABELS,
  WAITING_GRAB: '待接单',
}

/** v2 版状态中文（旧页词表 + v2 覆盖；未知枚举原样回显，与 orderStatusLabel 同式）。 */
export function v2OrderStatusLabel(status: string): string {
  return isOrderStatus(status) ? V2_ORDER_STATUS_LABELS[status] : status
}
