/**
 * 手机端 `redirect` 的站内校验（spec §5.1）。与桌面 `router/safe-redirect.ts` 的差异（不能直接复用的原因）：手机端挂在站点的 `/m/` 子路径下，**桌面路径（如 `/orders`）对手机端路由表是不存在的目标**，放行它会让登录成功后落到 404 兜底 ⇒ 收窄为**只放行以 `/m/` 开头的站内路径**，其余一律 `null`（调用方回落 `m-home`）。
 * 必须挡住：外站 URL 与协议相对 `//evil.com`、桌面路径、`/m\` 反斜杠把戏（WHATWG URL 把 `\` 当路径分隔）；`/m//evil.com` 与编码形态 `%2f%2f`（前缀之后的首字符不许是 `/`，raw 与解码后各查一遍）；`/m/../..` 相对段爬出（路径段里 `.` / `..` 一律拒）。编码形态也要查：调用方可能传未解码值 ⇒ `decodeOnce` 的结果再查一遍，解码失败（裸 `%`）回退原串。
 * ⚠️ 返回值只允许交给 `router.replace/push`（先过 `mRoutePathOf` 转回路由路径），**禁止** `location.assign/href`。
 */
export const M_SITE_BASE = '/m/'

/**
 * 路由路径 → 站点路径。守卫存 `redirect` 时用：`vue-router` 的 `fullPath` **不含** base
 * （`createCurrentLocation` 会 `stripBase`；vue-router 5.3.1 读源码实证），而这里的判据是「站点路径以 `/m/` 开头」⇒ 存之前要补上前缀。例：`/hall?game=3` → `/m/hall?game=3`。
 */
export function mSitePath(routePath: string): string {
  return `${M_SITE_BASE.replace(/\/$/, '')}${routePath}`
}

/**
 * 站点路径 → 路由路径（**只**对 `sanitizeMRedirect` 的返回值调用）：
 * `/m/hall?game=3` → `/hall?game=3`。`router.replace` 吃的是不含 base 的路由路径。
 */
export function mRoutePathOf(sitePath: string): string {
  return sitePath.slice(M_SITE_BASE.length - 1)
}

export function sanitizeMRedirect(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  // raw 与「解码一次」的结果都必须过同一套判据：编码形态不许绕过任何一条。
  if (!isSafeMPath(raw) || !isSafeMPath(decodeOnce(raw))) return null
  return raw
}

function isSafeMPath(value: string): boolean {
  // 前缀判据一举挡住：外站 URL、协议相对 `//`、桌面路径、`/m\…`（反斜杠不在前缀里）。
  if (!value.startsWith(M_SITE_BASE)) return false
  // WHATWG URL 会剥离 TAB/CR/LF、把 `\` 当路径分隔符——都能把站内相对路径变成跨源（桌面同款判据）。
  if (hasUnsafeChars(value)) return false
  // 前缀之后不许再以 `/` 开头：挡 `/m//evil.com`（转回路由路径会变 `//evil.com` 形态）。
  if (value.slice(M_SITE_BASE.length).startsWith('/')) return false
  // query/hash 之外的路径段里出现 `.` / `..` = 想爬出 `/m/`（如 `/m/../..`）→ 拒。
  const pathOnly = value.split(/[?#]/)[0] ?? ''
  return !pathOnly.split('/').some((seg) => seg === '.' || seg === '..')
}

function hasUnsafeChars(value: string): boolean {
  return /[\\\u0000-\u001f\u007f]/.test(value)
}

function decodeOnce(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}
