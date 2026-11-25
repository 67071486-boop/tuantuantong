import { ElMessage } from 'element-plus'
// 显式 import ElMessage **不会**带上组件样式（spec §7.4）：少了这一行就是「弹窗出来但没有样式」。
// 走 Element Plus 自己的按需样式入口，它会连带引入 base 与 badge（message 内部用到）。
import 'element-plus/es/components/message/style/css'
import { AuthExpiredError, BizError, HttpError, NetworkError } from './errors'

/** 把类型化错误变成用户可见提示。`api/` 的其余部分不依赖 UI，也不碰 store。 */
export function notifyError(e: unknown): void {
  if (e instanceof HttpError && e.csrfRefreshed) {
 // 文案**写死在这条分支里**，与 `api/http.ts` 构造该错误时的 message 是同一句；两层各写一遍是刻意的：
 // **本条分支写死** ⇒ 即使 `api/http.ts` 那一侧的 message 换了措辞，用户也不会看到服务端
 // 403 的原文（DRF 英文 `CSRF Failed: …`）。（⛔ 不写「任一层改动都不会漏出原文」
 // 这类未被枚举证明的绝对句。）
    ElMessage.error('会话校验已刷新，请重试')
    return
  }
  if (e instanceof AuthExpiredError || e instanceof BizError || e instanceof HttpError || e instanceof NetworkError) {
    ElMessage.error(e.message)
    return
  }
  ElMessage.error(e instanceof Error ? e.message : '未知错误')
}

/** 中性提示（不是错误）。用在对标物用 `layer.msg` 的地方，例如「请勾选要操作的数据行」。
 * 与 `notifyError` 分开，是为了不把「操作没做」伪装成「操作失败了」——
 * 颜色与语义都不同，混用会让用户以为系统出了故障。 */
export function notifyTip(msg: string): void {
  ElMessage.warning(msg)
}
