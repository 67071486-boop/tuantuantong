/**
 * 把一段文本写进剪贴板（**两条路**，B2）。
 *
 * 为什么要有这一层：`navigator.clipboard.writeText` 只在**安全上下文**（https / localhost）里存在，
 * 而本系统的租户站点有 `http://` 的内网域名（`docs/RUNBOOK.md` §3.0.1 的两台租户机就是这种访问
 * 形态之一）⇒ 直接调它会静默失败（本仓早先的处置是「不存在就提示一句失败」，见
 * `order-orderlist.ts::copyToClipboard` 的原注释）。
 *
 * 现在的口径（业务方 2026-10-28）：
 *  1. 有 `navigator.clipboard.writeText` ⇒ 用它；被拒（`NotAllowedError` / 无用户手势）⇒ 走 2；
 *  2. **兜底** `document.execCommand('copy')`：临时塞一个不可见的 `<textarea>`、选中、复制、清掉。
 *     ⚠️ 它必须在**用户手势的同步栈**里跑（`async` 里隔了 await 之后有的浏览器会拒），
 *     所以第 2 条只在第 1 条**同步失败或不存在**时才走——两条路都在一次调用里，不跨 tick 拆开。
 *
 * 返回值是「成没成」，⛔ **不 reject**：调用方只需要一句「已复制 / 复制失败」的提示。
 */
export function copyTextToClipboard(text: string): Promise<boolean> {
  if (typeof text !== 'string' || text === '') return Promise.resolve(false)
  const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
    typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (clipboard !== undefined && typeof clipboard.writeText === 'function') {
    try {
      // `Promise.resolve(...)` 把「替身返回 undefined / 真身返回 promise」两种形状都收下。
      return Promise.resolve(clipboard.writeText.call(clipboard, text)).then(
        () => true,
        () => copyWithExecCommand(text),
      )
    } catch {
      // 同步抛（方法存在但实现直接 throw）也要落到兜底那条路上。
      return Promise.resolve(copyWithExecCommand(text))
    }
  }
  return Promise.resolve(copyWithExecCommand(text))
}

/**
 * 兜底：`document.execCommand('copy')`。
 *
 * 手法照老规矩（临时 `<textarea>` + `select()` + `execCommand`），三条细节都有原因：
 *  - `position:fixed` + 视口外 + `opacity:0`：不占排版、不让页面跳一下（`display:none` 选不中）；
 *  - 复制完**先还原用户原来的选区**，再移除节点：否则用户的选区会莫名消失；
 *  - `execCommand` 不存在 / 抛错 / 返回 `false` 一律当**没成功**（⛔ 不假装复制成功）。
 */
function copyWithExecCommand(text: string): boolean {
  if (typeof document === 'undefined' || typeof document.execCommand !== 'function') return false
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '-9999px'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  const selection = typeof document.getSelection === 'function' ? document.getSelection() : null
  const previous =
    selection !== null && selection.rangeCount > 0 ? selection.getRangeAt(0) : null
  document.body.appendChild(textarea)
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)
  let ok = false
  try {
    ok = document.execCommand('copy') === true
  } catch {
    ok = false
  }
  textarea.remove()
  if (previous !== null && selection !== null) {
    selection.removeAllRanges()
    selection.addRange(previous)
  }
  return ok
}
