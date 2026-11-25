/**
 * 后端**成功** `msg` 的展示策略（2026-10-28 业务方口径）。
 *
 * 背景：指派打手 / 抢单成功后，后端若发现这单还挂着代练通订单会自动撤。
 * **撤不掉时接口仍是成功码**（指派/抢单确实生效了），只在 `msg` 里带明确警告：
 * 「代练通订单未能撤回（原话：…），请先到『第三方平台订单管理』撤回代练通平台订单，再指派打手」。
 * ⇒ 页面必须把 `msg` 原文显示出来；带这句警告时**不能一闪而过**（要人工确认）。
 *
 * 三个消费面（管理订单页 / 内部接单大厅 / 小程序接单大厅）共用这一份判定与文案，
 * ⛔ 别在各页各写一遍「怎么算警告、显示多久」。
 */

/** 警告句的稳定子串（业务方原话里那句；后端换前后缀也命中）。 */
export const PLATFORM_WITHDRAW_WARNING_TOKEN = '未能撤回'

/** 这条成功 `msg` 是不是「代练通订单撤不掉」的警告。 */
export function isPlatformWithdrawWarning(message: string): boolean {
  return message.includes(PLATFORM_WITHDRAW_WARNING_TOKEN)
}

/** 普通成功提示想停留多久（毫秒）：长句要读得完，比 `islandMsg` 的 1500 长）。 */
export const BACKEND_MESSAGE_TOAST_MS = 6000

/** 提示出口（layui：`layer.msg` / `layer.alert`；小程序：TDesign `Toast`）。 */
export type BackendMessageActions = {
  /** 轻提示；`durationMs` 缺省由调用方决定（layui 1500 / 小程序默认）。 */
  toast: (text: string, durationMs?: number) => void
  /** 需要用户确认的警告（停留更久 / 弹层）。 */
  alert: (text: string) => void
}

/**
 * 指派 / 抢单成功后的提示：**优先原样显示后端 `msg`**。
 *
 * | msg | 展示 |
 * |---|---|
 * | 空串 | `fallback`（如「指派成功」/「抢单成功」的默认文案） |
 * | 带「未能撤回」警告 | `actions.alert`（人工确认，⛔ 不一闪而过） |
 * | 其它非空 | `actions.toast`，停留 `BACKEND_MESSAGE_TOAST_MS` |
 *
 * ⚠️ **顺序**：调用方一律先 `reload()` 再调本函数（本页既有口径：`reload()` 里的筛选警告
 * 会被后来的成功提示顶掉，反过来则会吞掉成功提示）。
 */
export function showBackendSuccessMessage(
  actions: BackendMessageActions,
  message: string,
  fallback: string,
): void {
  if (message.trim() === '') {
    actions.toast(fallback)
    return
  }
  if (isPlatformWithdrawWarning(message)) {
    actions.alert(message)
    return
  }
  actions.toast(message, BACKEND_MESSAGE_TOAST_MS)
}
