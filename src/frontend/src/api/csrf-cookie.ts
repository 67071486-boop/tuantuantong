export const CSRF_COOKIE_NAME = 'csrftoken'

/** 读 `csrftoken` 的匹配式。名字取自上面的常量（纯 ASCII 标识符，无正则转义风险）。
 * 两处各写一遍名字的话，将来改名字只会改一半，而症状是**静默**的（永远读不到 token →
 * 所有写请求 403）；`csrf-cookie.spec.ts` 有「常量与正则同源」一条把它钉住。 */
const CSRF_COOKIE_RE = new RegExp(`(?:^|;\\s*)${CSRF_COOKIE_NAME}=([^;]*)`)

/** 读 `csrftoken` cookie 的**当前**值。抽成独立模块是为了让 `http.spec.ts` 整块 mock 它，从而留在 `environment: 'node'`（见 spec §4.3）。⚠️ **每次请求现读、不要缓存**：Django 的 `login()` / `alogin()` 都会 `rotate_token`（`django/contrib/auth/__init__.py:196` 与同文件 `:239`），登录后 cookie 值即变——缓存 = 登录后所有写请求永久 403，且重取救不回来。（出处更正：spec §4.3-1 把这处记成「`login()`/`logout()` … `:196,239`」。按 Django 5.2.17源码，`:239` 是 `alogin()`、不是 `logout()`；`logout()` 自 `:243` 起，只 `session.flush()`、不调 `rotate_token`。结论不变——驱动"不要缓存"的是 `login()` 那一行。已回报控制方修 spec 文案。）
 * 可读性依据 Django 默认（不是本项目配置）：`global_settings.py:570`（名字）、`:573`（path=/）、`:575 CSRF_COOKIE_HTTPONLY = False`、`:579 CSRF_USE_SESSIONS = False`。 */
export function readCsrfCookie(): string | null {
  const m = CSRF_COOKIE_RE.exec(document.cookie)
  const raw = m?.[1]
  return raw ? decodeURIComponent(raw) : null
}
