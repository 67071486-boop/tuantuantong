/**
 * 把文本写进剪贴板（H5 移动端）——**带降级**：有 `navigator.clipboard.writeText` 就用它，不可用 / 被拒 ⇒ 落到 `document.execCommand('copy')`（离屏 textarea + select），降级口径照桌面 `self-registration.ts::copyToClipboard`。
 * ⚠️ **降级不是可选项**：线上移动端跑在 **http** 内网域名下，`navigator.clipboard` 只在安全上下文（https / localhost）存在 ⇒ 只写前者的版本在真机必然失败。⛔ 不 import 桌面那份（cross-layer import 会把 island 页连同证据件依赖拖进移动端包）；代价如实登记：两处同形小函数，改一处要想到另一处。
 */

/** 老接口兜底：离屏 textarea + `execCommand`。`display:none` 会让 execCommand 失败 ⇒ 移到视口外。 */
function legacyCopy(text: string): boolean {
  const el = document.createElement('textarea')
  el.value = text
  el.setAttribute('readonly', '')
  el.style.position = 'fixed'
  el.style.top = '-1000px'
  el.style.opacity = '0'
  document.body.appendChild(el)
  el.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(el)
  return ok
}

/** 复制文本；返回**真实**成败（调用方据此选文案，⛔ 不许假装成功）。 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 非安全上下文 / 无权限 ⇒ 落到 execCommand
    }
  }
  return legacyCopy(text)
}
