/**
 * 手机 UA 访问桌面端 ⇒ 自动引到 `/m/`（纯函数判定 + 入口一行调用）。放在前端入口而非 Django/nginx：生产页面路由全由 nginx 静态直出、Django 看不到页面请求，⛔ 不改 nginx ⇒ 唯一执行点是桌面 SPA 入口。
 * 判据：经典移动端 UA 词族（大小写不敏感，手写正则、⛔ 不为此引运行时依赖——够用判定，不是设备识别库）；已在 `/m/` ⇒ 不跳；⚠️ iPadOS 13+ Safari 自报 Macintosh 桌面 UA ⇒ 接不住（如实登记的边界，要接得住得引 `navigator.maxTouchPoints`，纯函数就测不了）。
 * 抑制标记住 `sessionStorage`（换标签页要重判、同页来回要压住）：守卫弹非打手回桌面前写、打手进 `/m/` 或 `stores/session.ts::clear()` 时清。⚠️ 写失败 = 标记永远不在 = 桌面↔`/m/` 来回跳 ⇒ 调用方必须写完自证、读不到就不弹；本模块只管读写，⛔ 不在这里吞掉这个区别。
 */

/** 手写一份并注明依据，⛔ 不为此引一个运行时依赖（UAParser 之类）——这是一个**够用**的判定，不是设备识别库。 */
const MOBILE_UA_RE =
  /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|Mobile/i

export function isMobileUserAgent(ua: string): boolean {
  return MOBILE_UA_RE.test(ua)
}

/** 抑制标记在 `sessionStorage` 里的键（本模块唯一出处；守卫与入口都经下面的函数读写它）。 */
export const MOBILE_REDIRECT_SUPPRESS_KEY = 'tt.mobile_redirect_suppressed'

/** 标记存在时写的值（只有存在与否有语义，值本身只为可读性）。 */
const SUPPRESS_VALUE = '1'

/**
 * 读写标记所需的最小 storage 形状：真 `Storage` 与测试替身都满足它
 * ⇒ 注入点不必伪造整个 `Storage` 接口。
 */
export type MobileRedirectStorage = {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

/** 默认 storage：`window.sessionStorage`；取不到（非浏览器环境 / 隐私模式抛错）⇒ `null`。 */
function defaultStorage(): MobileRedirectStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

/** 读抑制标记；取不到或读失败 ⇒ `false`（= 没有标记，照旧判定）。 */
export function readMobileRedirectSuppressed(
  storage: MobileRedirectStorage | null = defaultStorage(),
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(MOBILE_REDIRECT_SUPPRESS_KEY) === SUPPRESS_VALUE
  } catch {
    return false
  }
}

/**
 * 写抑制标记（非打手被弹回桌面之前调一次）；存储不可用时静默。
 * ⚠️ 写完必须自证（调用方紧接着 `readMobileRedirectSuppressed()`）：写失败 = 标记永远不在 ⇒ 桌面入口还会跳回 `/m/` ⇒ 死循环。
 */
export function writeMobileRedirectSuppressed(
  storage: MobileRedirectStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.setItem(MOBILE_REDIRECT_SUPPRESS_KEY, SUPPRESS_VALUE)
  } catch {
    // 有意留空：这里只吞「存储不可用」，⛔ 不当作成功——成不成由调用方读一次来判。
  }
}

/** 清抑制标记（打手进 `/m/`、或会话终结 `stores/session.ts::clear()` 时调）；存储不可用时静默。 */
export function clearMobileRedirectSuppressed(
  storage: MobileRedirectStorage | null = defaultStorage(),
): void {
  if (!storage) return
  try {
    storage.removeItem(MOBILE_REDIRECT_SUPPRESS_KEY)
  } catch {
    // 有意留空：清不掉的后果 = 本标签页少跳一次 `/m/`（会话照旧判定），不是安全问题。
  }
}

/**
 * 判定「这条 UA + 当前路径」该不该跳 `/m/`（纯函数，可测）。
 * `suppressed` = 「本标签页已经被请回过桌面，别再跳」⇒ 一律 `false`；默认 `false`，既有两参调用语义逐字不变。
 */
export function shouldRedirectToMobile(
  ua: string,
  pathname: string,
  suppressed: boolean = false,
  search: string = '',
): boolean {
  if (typeof ua !== 'string') return false
  if (typeof pathname !== 'string') return false
  if (suppressed) return false
  if (pathname.startsWith('/m/')) return false
  if (/^\/visitor-chat\/?$/i.test(pathname)) return false
  const approvals = '/v2/approvals'
  if (pathname === approvals) return false
  if (pathname === '/login' && new URLSearchParams(search).get('redirect') === approvals) return false
  return isMobileUserAgent(ua)
}

/**
 * `main.ts` 的那一行调用：该跳就 `location.replace('/m/')`（不产生历史记录，返回可回退）。
 *
 * 第三参默认从 `sessionStorage` 现读（= 真实入口行为）；测试要固定它时可以显式传值。
 */
export function redirectToMobileIfMobile(
  ua: string,
  pathname: string,
  suppressed: boolean = readMobileRedirectSuppressed(),
): void {
  if (!shouldRedirectToMobile(ua, pathname, suppressed, window.location.search)) return
  window.location.replace('/m/')
}
