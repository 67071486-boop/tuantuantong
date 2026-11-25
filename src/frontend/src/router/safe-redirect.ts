/**
 * `redirect` 的站内校验（spec §5.2 的 payload 表）。为什么不是「以 `/` 开头且不以 `//` 开头」就够：① `vue-router` 的 `route.query` **已经 `decodeURIComponent` 过**，`/%5c` 这类会在此处还原成 `\`；② WHATWG URL 会把 `\` 与 TAB/CR/LF 当路径分隔或剥离，`/\evil.example` 解析出来是**跨源**；③ 重复 query 参数会解成**数组**。返回值只允许交给 `router.replace/push`——**禁止** `location.assign/href`：vue-router 在 `pushState` 抛错时会回退到浏览器级导航。
 * **编码形态也要查**：payload 表把 `/%5cevil.example`、`/%09/evil.example` 直接列为攻击输入，而正常路径上 `route.query` 只给到**解码后**的 `\` 与 TAB——只查原始字符时编码形态会被原样放行（实测：只查原始字符的版本跑这张表 = 2 failed | 12 passed）。这里对调用方传未解码值的情形做纵深防御：**解码失败（裸 `%`）回退原串**，不因异常放大成拒绝之外的后果。
 * ⚠️ **双重编码（`%252F…`）本条不拒绝**（只解一次，解完是 `%2F`、无 `\` 与 C0 字符）——它是**表外形态**，行为与落点由 `__tests__/safe-redirect.spec.ts` 的一条用例**钉住**：落点必须仍恰好一个前导 `/`。这条注释不写「已实测同源」这种绝对话，因为能证明它的只有那条用例。
 */
export function sanitizeRedirect(raw: unknown, resolve: (path: string) => boolean): string | null {
  if (typeof raw !== 'string') return null
  if (!/^\/(?!\/)/.test(raw)) return null
  if (hasUnsafeChars(raw) || hasUnsafeChars(decodeOnce(raw))) return null
  if (raw === '/login' || raw.startsWith('/login?') || raw.startsWith('/login/')) return null
  if (!resolve(raw)) return null
  return raw
}

/** WHATWG URL 会剥离 TAB/CR/LF、把 `\` 当路径分隔符 —— 两者都能把站内相对路径变成协议相对 URL。 */
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
