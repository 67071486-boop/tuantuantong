import { toLocalIso, type OrderSearchParams } from '@/api/orders'

/**
 * 筛选表单。**默认露的 6 个高频控件 + 高级筛选里的 2 个**（spec §4.2）。⚠️ 四个下拉（`status` / `game` / `is_urgent` / `server`）与两个日期是**可清空的 Element Plus 控件**，清空时它们写回来的值**不是** `null`，实测（在 `node_modules/element-plus` 里读到的实现）：`el-select` → `undefined`、`el-date-picker` → `null`、`el-input` → `''`。
 * 所以这几项的类型都带上 `undefined` / `null`，判空统一走 `orNull` / `orEmpty`——只判 `!== ''` 会让「清空后的日期」走进 `new Date('nullT00:00:00')`（Invalid Date）、送出一串 `NaN-NaN-NaNT...` 给后端；只判 `!== null` 会让「清空后的下拉」被当成「筛过」，空态文案从此永远显示「没有匹配的订单」（spec §4.2 要求两种空态可区分）。
 */
export type OrderFilterForm = {
  keyword: string
  status: string | undefined
  game: number | null | undefined
  project_text: string
  /** `null` / `undefined` = 不限；`true` / `false` 都是**有效筛选值**。 */
  is_urgent: boolean | null | undefined
  date_from: string | null | undefined
  date_to: string | null | undefined
  /** 高级筛选 */
  server: number | null | undefined
  tag: string
}

export function emptyFilterForm(): OrderFilterForm {
  return {
    keyword: '',
    status: '',
    game: null,
    project_text: '',
    is_urgent: null,
    date_from: '',
    date_to: '',
    server: null,
    tag: '',
  }
}

/** `localStorage` 的键。**带页面前缀**，避免与将来的其他页面撞名。 */
export const ADVANCED_FILTER_KEY = 'tt.orders.filters.advancedExpanded'

/** 只依赖 Storage 接口（不是 `window.localStorage`），所以可以注入假实现直接测。 */
export type StorageLike = Pick<Storage, 'getItem' | 'setItem'>

/** `undefined` → `null`（剩下的判断只需要对付两种「空」）。 */
function orNull<T>(value: T | null | undefined): T | null {
  return value === undefined ? null : value
}

/** `null` / `undefined` → `''`。字符串字段一律先过它，后面的 `.trim()` 才不会炸。 */
function orEmpty(value: string | null | undefined): string {
  return typeof value === 'string' ? value : ''
}

/**
 * 高级筛选的展开状态**记忆在 localStorage**（spec §4.2）。
 * 读不到 / 值不是 `'0'` / `'1'` 时一律回退到**收起**——`localStorage` 里可能是别人写的、也可能被手改。
 */
export function readAdvancedExpanded(store: StorageLike): boolean {
  try {
    return store.getItem(ADVANCED_FILTER_KEY) === '1'
  } catch {
    return false
  }
}

export function writeAdvancedExpanded(store: StorageLike, value: boolean): void {
  try {
    store.setItem(ADVANCED_FILTER_KEY, value ? '1' : '0')
  } catch {
    // Safari 隐私模式 / 存储被禁用：记忆失败不影响筛选本身。
  }
}

/**
 * 表单 → 查询参数；时间范围按**本地日**取整（spec §3 第 5 条）：`date_from` 取当日 `00:00:00`、`date_to` 取**当日 `23:59:59`**。
 * 后端是 `created_at__lte`（`selectors.search_orders`），「到某天」必须落到**当天最后一秒**，否则当天的订单会被整天漏掉（用「次日 00:00:00 + `__lt`」也可以，但那要改后端语义——前端不改后端）。送出去的一律是**带时区偏移**的 ISO 8601（`toLocalIso`），不是裸日期。
 */
export function toSearchParams(
  form: OrderFilterForm,
  page: number,
  pageSize: number,
): OrderSearchParams {
  const params: OrderSearchParams = { page, page_size: pageSize }
  const keyword = orEmpty(form.keyword).trim()
  if (keyword !== '') params.keyword = keyword
  const status = orEmpty(form.status)
  if (status !== '') params.status = status
  const game = orNull(form.game)
  if (game !== null) params.game = game
  const projectText = orEmpty(form.project_text).trim()
  if (projectText !== '') params.project_text = projectText
  // ⚠️ 判据是「不是空」而不是真值：`is_urgent: false` 是「只看不加急」，是一条真实筛选条件。
  const urgent = orNull(form.is_urgent)
  if (urgent !== null) params.is_urgent = urgent
  const tag = orEmpty(form.tag).trim()
  if (tag !== '') params.tag = tag
  const server = orNull(form.server)
  if (server !== null) params.server = server
  const dateFrom = orEmpty(form.date_from)
  if (dateFrom !== '') params.date_from = toLocalIso(new Date(`${dateFrom}T00:00:00`))
  const dateTo = orEmpty(form.date_to)
  if (dateTo !== '') params.date_to = toLocalIso(new Date(`${dateTo}T23:59:59`))
  return params
}

/**
 * 「有没有在筛」——**空态文案靠它区分**（spec §4.2 / §7）：
 * 筛了却一条没有 = 「没有匹配的订单」；没筛就是没有 = 「还没有订单」。
 * 这两个文案必须不同：把「没有匹配」写成「还没有订单」会让客服以为数据丢了。
 */
export function hasActiveFilters(form: OrderFilterForm): boolean {
  return (
    orEmpty(form.keyword).trim() !== '' ||
    orEmpty(form.status) !== '' ||
    orNull(form.game) !== null ||
    orNull(form.server) !== null ||
    orEmpty(form.project_text).trim() !== '' ||
    orNull(form.is_urgent) !== null ||
    orEmpty(form.tag).trim() !== '' ||
    orEmpty(form.date_from) !== '' ||
    orEmpty(form.date_to) !== ''
  )
}

export function emptyStateText(total: number, form: OrderFilterForm): string {
  if (total > 0) return ''
  return hasActiveFilters(form) ? '没有匹配的订单' : '还没有订单'
}

/**
 * 给出短中文标签的动作 code（取值是 `OperationLog.action` 的 code）。表外的动作由 `auditActionLabel` 原样回退成 raw code。
 * 生产侧已无消费者（订单详情换成 island 页后删了唯一引用），测试和 `api/orders.ts` 仍把它当短中文标签的出处，所以类型留着。
 */
export type LabelledAuditAction = 'order.transition' | 'order.change_taker_price' | 'performance.review'

const AUDIT_ACTION_LABELS: Record<LabelledAuditAction, string> = {
  'order.transition': '状态变更',
  'order.change_taker_price': '改价',
  'performance.review': '绩效审核',
}

/**
 * 留痕的动作名：登记过的翻成中文，**其余原样返回 raw code**（不抛错、不空白）。
 * ⚠️ 判据用 `Object.prototype.hasOwnProperty.call`，**不是** `action in AUDIT_ACTION_LABELS`：`in` 会走原型链，`'toString' in {}` 为真 ⇒ 一个与 `Object.prototype` 同名的动作会被渲染成 `function toString() { [native code] }`（有测钉着这条回退）。
 * 不用 `Object.hasOwn`：它要 ES2022，而 `tsconfig.vitest.json` 把 `lib` 清空后由 `@types/node` 兜到 ES2020，`pnpm typecheck` 会报 TS2550。
 */
export function auditActionLabel(action: string): string {
  return Object.prototype.hasOwnProperty.call(AUDIT_ACTION_LABELS, action)
    ? AUDIT_ACTION_LABELS[action as LabelledAuditAction]
    : action
}
