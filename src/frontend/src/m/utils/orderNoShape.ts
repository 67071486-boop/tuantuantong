/**
 * 手机端订单详情的单号**形状**闸：进入页面即验，不符 → 404 态页、**零请求**。语义照桌面 `api/orders.ts::isOrderNoShape`（20 位纯数字，正则逐字同形）；
 * 抽成本目录纯函数而不直接 import（照 `hallDisplay.ts` / `hallAutoRefresh.ts` 的先例）：手机端页面的判据落在 `m/utils/` 的可单测纯函数上，渲染期与请求期共用同一判据，不会漂移。
 * ⚠️ 不在 `api/myOrders.ts::fetchMyOrderDetail` 里判（与 `submitMyOrder` 刻意相反）：detail 走宽松共享转换器（只挡控制字符），畸形但干净的单号会落**统一信封**的业务失败，不存在「HTML 404 不可读」——完整理由见该函数 docstring。
 */

/** 与桌面 `api/orders.ts::ORDER_NO_RE` 同形：20 位纯数字。 */
const ORDER_NO_RE = /^\d{20}$/

/**
 * `value` 是不是合法单号形状。入参收 `unknown`：vue-router 的 `params` 类型上是
 * `string | string[]`，页面在判据里收窄而不是把未定形状送进正则（契约漂移由本函数挡下）。
 */
export function isOrderNoShape(value: unknown): boolean {
  return typeof value === 'string' && ORDER_NO_RE.test(value)
}
