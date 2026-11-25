/**
 * 从 layui 取数失败时拿到的响应里取**后端那句 `msg`**（统一信封 `{code,msg,data}`）。
 *
 * ⚠️ **为什么两层都要读**——这是实测，不是推理：
 * layui 的 `table.render({error})` 回调第一参在文档/直觉里是「响应体」（我们的信封
 * `{code,msg}`），但**真实浏览器里传进来的是 jqXHR 对象**，实测形状为
 * `{readyState, responseText, responseJSON:{code,msg,data}, status, statusText}`。
 * 只读 `res.msg` 会拿到 `undefined` ⇒ 界面回退成 layui 自带的那句英文 `error`
 * （等于把权限失败吞掉，见 `docs/FRONTEND-REPLICA-STANDARD.md` 的自检清单）。
 * 所以：① 先读信封本身；② 再读 jqXHR 的 `responseJSON.msg`；③ 最后退回解析
 * `responseText`（jQuery 风格的 XHR 不一定都给 `responseJSON`，本项目不赌第三方行为）。
 *
 * 形状都认不出来时（`Error`、HTML 错误页、`null`…）给一句**中性中文**兜底，
 * **绝不回退成英文 `error`**。调用方若另有状态码文案（如 `HTTP 500`），用
 * `BACKEND_ERROR_FALLBACK` 判一下即可，见 `order-insiderecorderlist.ts`。
 */
export const BACKEND_ERROR_FALLBACK = '请求失败，请稍后重试'

/** 读不出后端 `msg` 时的中性兜底（**不含英文 `error`**）。 */
export function backendErrorMessage(res: unknown): string {
  if (res instanceof Error) return BACKEND_ERROR_FALLBACK
  if (typeof res !== 'object' || res === null) return BACKEND_ERROR_FALLBACK
  const box = res as {
    msg?: unknown
    responseJSON?: { msg?: unknown }
    responseText?: unknown
  }
  // ① 统一信封本身：`{"code":403,"msg":"只有代练员可以抢单","data":null}`。
  const direct = box.msg
  if (typeof direct === 'string' && direct.trim() !== '') return direct
  // ② 真实浏览器里的 jqXHR：msg 在 `responseJSON.msg`（`res.msg` 不存在）。
  const nested = box.responseJSON?.msg
  if (typeof nested === 'string' && nested.trim() !== '') return nested
  // ③ 抢单大厅那条线补的形状：连 `responseJSON` 都没有时，退回解析 `responseText`。
  if (typeof box.responseText === 'string' && box.responseText.trim() !== '') {
    try {
      const parsed: unknown = JSON.parse(box.responseText)
      const fromText =
        typeof parsed === 'object' && parsed !== null
          ? (parsed as { msg?: unknown }).msg
          : undefined
      if (typeof fromText === 'string' && fromText.trim() !== '') return fromText
    } catch {
      // 非 JSON（HTML 错误页）：交给下面的中性兜底。
    }
  }
  return BACKEND_ERROR_FALLBACK
}
