/**
 * 手机端的 ISO-8601 时间显示纯函数（我的单卡片「接手时间」、详情页 `claimed_at` 共用）。判据 = 按**串里自带的偏移**换算到项目时区再取墙钟：
 * 本仓 ISO 串有两族时区口径——DRF `DateTimeField` 端点按项目时区序列化（尾部 `+08:00`），模型直读 `isoformat()` 的端点是 UTC（`+00:00`），写死任何一侧都会错 8 小时（实测先例与证据链见 `order-orderview.ts::formatLogTime`）。
 * 实现口径：只用 `Date.UTC(...)` 与 `getUTC*`，⛔ 不用 `new Date(串)` + `getHours()`（那按浏览器时区渲染，东八区以外的设备整点平移）；无偏移尾部的串按「已是项目时区墙钟」原样取（与 `api/auditLogs.ts::formatCreatedAt` 同口径）；形状认不出 ⇒ **空串**，绝不把原始串填进卡片。
 */

/** 项目 `TIME_ZONE`（`src/config/settings.py` 的 `Asia/Shanghai` = UTC+8，无夏令时）。 */
const DISPLAY_OFFSET_MINUTES = 8 * 60

/** ISO-8601 → `YYYY-MM-DD HH:mm`（项目时区墙钟）。 */
export function formatDateTime(value: unknown): string {
  if (typeof value !== 'string') return ''
  // 秒与小数秒可选（后端可能回 `2026-10-10T00:14:40.110591+00:00`）——必须一起吃掉，
  // 否则 slice 出的「剩余串」以 `:` 开头，偏移认不出来（会静默按「无偏移」处理）。
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?/.exec(value)
  if (m === null) return ''
  const rest = value.slice(m[0].length)
  let offsetMinutes: number | null
  if (/^Z/i.test(rest)) {
    offsetMinutes = 0
  } else {
    const om = /^([+-])(\d{2}):?(\d{2})/.exec(rest)
    offsetMinutes =
      om === null ? null : (om[1] === '-' ? -1 : 1) * (Number(om[2]) * 60 + Number(om[3]))
  }
  // `null` = 串里没给偏移 ⇒ 视为已经是项目时区的墙钟（位移 0）。
  const shift = offsetMinutes === null ? 0 : DISPLAY_OFFSET_MINUTES - offsetMinutes
  const at = new Date(
    Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5])) +
      shift * 60_000,
  )
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())} ${pad(
    at.getUTCHours(),
  )}:${pad(at.getUTCMinutes())}`
}
