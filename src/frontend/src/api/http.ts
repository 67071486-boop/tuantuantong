import axios, { AxiosError, type AxiosRequestConfig } from 'axios'
import { readCsrfCookie } from './csrf-cookie'
// ⚠️ **有意为之的循环依赖**：`http.ts` → `auth.ts`（本行的 `fetchCsrf`）与 `auth.ts` → `http.ts`
// （`request`）互为 import（spec 的预检把它裁决为「接受」，见计划的进度账本）。
// 它安全的前提是**两边都只在函数体内引用**（本文件的 `refreshCsrfToken()`、auth.ts 的四个封装
// 函数），**模块顶层不得调用**：ESM 的循环绑定在模块求值期间只是「尚未初始化的引用」，只在
// 函数体内用就不会在求值期读到它。
// 这不是靠注释自证：`http.spec.ts` 的「cookie 缺失 → 先引导再发原请求（有序）」走的就是
// `request()` → `fetchCsrf()` → `request()` 这一整圈；环一旦解析失败（例如有人把 `fetchCsrf`
// 改成模块顶层求值的常量、或把 `request` 挪出本文件），那条用例会**直接红**，不会静默降级。
// 也**不要**为了「消除」环而在本文件自建 axios 实例 / 复制 token 端点的 URL：那会让重取绕过
// `request()` 的信封与 401 分类，正好破坏 spec §4.3-4。
import { fetchCsrf } from './auth'
import { NetworkError, mapHttpStatus, HttpError } from './errors'
import { isEnvelope, unwrap, type ApiResult } from './unwrap'
import { finishOrderFinanceRequest, orderFinancePath, prepareOrderFinanceRequest, wasOrderFinanceDirect } from './orderFinanceDirect'

/** 超时值是**拍的**（不是算出来的），写在这里以便一处调整。 */
const TIMEOUT_MS = 15_000

/** CSRF 注入用的请求头名。**已接线**（P5 前端）：写请求由`withCsrfHeader()` 每次现读 `csrftoken` Cookie 注入（读取见 `./csrf-cookie.ts`），失败时的重取与单飞见 `refreshCsrfToken()`。头名取自 Django 的 `settings.CSRF_HEADER_NAME`，默认值见`django/conf/global_settings.py:577`：`CSRF_HEADER_NAME = "HTTP_X_CSRFTOKEN"`，即 HTTP 头的 `X-CSRFToken`（`django/middleware/csrf.py:384` 就是从 `request.META` 取它）。后端 `CsrfViewMiddleware` 是开着的（`src/config/settings.py:58`），同源代理下游览器自动带 Cookie。 */
export const CSRF_HEADER_NAME = 'X-CSRFToken'

export const http = axios.create({
  // 同源：开发期由 Vite 代理转到 Django（见 vite.config.ts），因此 Cookie 自动携带、无 CORS。
  baseURL: import.meta.env.VITE_API_BASE ?? '',
  timeout: TIMEOUT_MS,
})
http.interceptors.request.use(prepareOrderFinanceRequest, undefined, {
  runWhen: config => Boolean(orderFinancePath(config.url ?? '', config.baseURL)),
})
http.interceptors.response.use(response => {
  finishOrderFinanceRequest(response.config, response.status)
  return response
}, (error: unknown) => {
  if (error instanceof AxiosError) finishOrderFinanceRequest(error.config, error.response?.status)
  throw error
})

/** 401 处理器。**默认 null = 什么都不做**，由 `main.ts` 这一个装配点用 `setUnauthorizedHandler()`
 * 注入（spec §4.2/§5.4：清会话 + 记 redirect + 跳登录都在注入的函数里）。
 * `http.ts` 因而不 import store / router / UI——状态与导航都是注入进来的，测试可直接注入 spy。 */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn
}

/** 需要 CSRF 头的方法：Django 只对非安全方法跑 `CsrfViewMiddleware`。 */
const UNSAFE_METHODS = new Set(['post', 'put', 'patch', 'delete'])

/** 重取失败 / 重取成功但仍读不到 cookie 时的报错文案（spec §4.3-4 要求"明确报错"）。 */
const CSRF_UNAVAILABLE_MSG = '无法获取 CSRF 令牌'

/** 单飞句柄：并发 N 个 CSRF 失败只打 **1** 次 token 端点（spec §4.3-3）。 */
let csrfInflight: Promise<void> | null = null

/** 单飞重取 token。重取**走 `fetchCsrf()`**——它本身就是一条 `request()` 调用，且带`skipUnauthorizedHandler: true`（见 `auth.ts`），所以重取再遇 401 不会回头触发 401 处理器互相打转。**不是**在这里递归调 `request()`。这里是本文件 ↔ `auth.ts` 那个**有意为之的循环依赖**的另一半（另一半是文件头的 `fetchCsrf`import）：`fetchCsrf` 只在**本函数体内**被调用，模块求值时不会碰那个绑定，因而是安全的——若有人把它提到模块顶层求值，`http.spec.ts` 的引导用例会直接红。`finally` 里复位句柄：不复位的话，下一次 CSRF 失败会复用这个**已完成**的 promise、永远拿不到新 token（`http.spec.ts` 的「顺序第二次失败会重新取 token」用例钉住这一点）。⚠️ **已知陷阱（今天不可达，改这附近前先读）**：`csrfInflight` 是「整段重取」的句柄。若重取
 * 请求**自身**也吃到 403 + CSRF 文案，它会走回 `request()` 的 CSRF 分支、再调本函数，而本函数交回的是**同一个**（外层尚未 settle 的）promise → 两边互等，**永不 settle**。已实测复现：把 adapter 桩成「所有请求都 403 + `CSRF 校验失败`」→ 用例 1.2s 超时（探针跑完即删）。今天为何不可达：重取是 GET，Django 在**校验 token 之前**就放行安全方法（`django/middleware/csrf.py:424-425` 的 `if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"): return self._accept(request)`），本项目守卫走 401，且 `fetchCsrf()` 带 `skipUnauthorizedHandler: true`。**不要**为了"防重入"在这里加 `refreshing` 模块标志：并发 403 会因此跳过重取，直接破坏 §4.3-3
 * 的单飞（`http.spec.ts` 的「并发 3 → token 端点恰 1 次」会红）。 */
function refreshCsrfToken(): Promise<void> {
  if (!csrfInflight) {
    csrfInflight = fetchCsrf()
      .then(() => undefined)
      .finally(() => {
        csrfInflight = null
      })
  }
  return csrfInflight
}

/** 重取一次即止：401/403/网络错误一律化成同一句明确报错，绝不递归（spec §4.3-4）。 */
async function refreshCsrfOrThrow(): Promise<void> {
  try {
    await refreshCsrfToken()
  } catch {
    throw new NetworkError(CSRF_UNAVAILABLE_MSG)
  }
}

/** 只认服务端两种已知文案，**匹配不上一律按"无权限"处理**（fail-safe，spec §5.4）：- 本项目 Django 层视图：`src/apps/core/views.py:38` 的 `"CSRF 校验失败"`；- DRF `SessionAuthentication.enforce_csrf`：`rest_framework/authentication.py:148` 的`'CSRF Failed: %s'`。后端改文案时前端会退化成「普通 403」（不清会话、不提示重试），所以两侧文案是跨层契约：后端那一侧由 `src/tests/test_p5_envelope_csrf.py:399` 的逐字节信封断言钉住；「cookie 可读」那一侧（`CSRF_COOKIE_HTTPONLY = False`、`CSRF_USE_SESSIONS = False`、cookie 名与请求头名）由 `src/tests/test_p5_envelope_csrf.py:103` 的 `test_csrf_cookie_is_readable_by_the_spa` 钉住
 * （已加）。 */
function isCsrfFailure(status: number, msg: string): boolean {
  return status === 403 && (msg === 'CSRF 校验失败' || msg.startsWith('CSRF Failed'))
}

function envelopeMsg(body: unknown): string {
  return isEnvelope(body) && typeof body.msg === 'string' ? body.msg : ''
}

/** 在**本地副本**上补 CSRF 头：基线从不改调用方传入的 `config`，`request()` 也不该开这个先例（调用方可能复用同一个 config 对象发第二次请求）。注入形状已实测：`{ ...config, headers: { ...config.headers, [CSRF_HEADER_NAME]: token } }` 能过类型检查，adapter 侧 `config.headers?.['X-CSRFToken']` 也读得到。 */
function withCsrfHeader(config: AxiosRequestConfig, token: string): AxiosRequestConfig {
  return { ...config, headers: { ...config.headers, [CSRF_HEADER_NAME]: token } }
}

/** 统一入口：返回解包后的 data；失败抛 `errors.ts` 的类型化错误。关键点：**错误响应不一定是 JSON 信封**——未预期异常时 Django 返回 HTML（spec §7.2），所以这里先判 isEnvelope，再决定怎么取 msg。本函数是 `api/` 下**业务信封请求**的统一传输/认证层出口：错误都在下面的catch 分支里分类，调用方只看到 `errors.ts` 的类型化错误。⚠️ 措辞边界（E-0011）：**别把它读成「`api/` 下的请求都从这里走」**——下面只给**一些例子，且并不穷举**；新增出口随时会让「还有没有别的」这类句子变成假话，所以这里刻意不写条数与因果。## 不走本函数的路径（**例如**，**列表非穷举**）- `api/performances.ts` 的 `downloadPerformanceExport`——成功响应**不是** `{code,msg,data}` 信封（是附件那种 **`.xlsx` 字节流**；原先的 `text/csv` 已过期，见该文件自己的「更正」块），所以它直接 `http.get<Blob>`（理由见那个函数自己的注释）。后果：它拿到的 401 **不会触发 `onUnauthorized`**（会话失效时不清会话、不跳登录页），调用方只看到`AuthExpiredError` 的中文提示。- `/healthz` 存活探针（**只作历史记录**）：返回 `JsonResponse({"status":"ok"})`，不是信封；它当初在前端的消费者是**已删的 EP 骨架页** `src/frontend/src/views/HomeView.vue`（2026-09-23 被 `/Index/console` 复刻页取代），该页删除后它在前端的调用点也随之消失。出处（含它当时不经 `onUnauthorized` 的后果）：`docs/specs/2026-09-20-p5-frontend-auth-design.md` §5.4。- **「说信封」也可能不走本函数**（可见「不走本函数」与「响应不是信封」并非同一件事）：
 * `layui-island/config-replica.ts` 的 `apiRequest` 用 `fetch` 直连、消费统一信封（它刻意只依赖无状态导出，见该文件自身注释）；layui 表格由 `table.render({ url })` 自己发 XHR、在`parseData` 里解统一信封（如 `layui-island/pages/order-orderlist.ts`）。⛔ 给后来的编辑者：**不要**在这里补「共 N 条」「都因为…」「唯一 / 那一处」这类完整性或因果断言——多一条出口就会让它们变成假话（E-0011 的缺陷形状）。要写就逐条枚举并核过，否则保持非穷举。非安全方法的三段处理（spec §4.3 的四条约束都落在这里）：1. 每次现读 `csrftoken` 并注进请求头（不缓存）；2. 读不到 → 先 `GET /api/auth/csrf` 引导一次，再发原请求；3. 原请求 CSRF 失败 → 单飞重取一次 token、**不重放原请求**，然后抛带 `csrfRefreshed` 的
 * `HttpError`（调用方提示"请重试"）；重取自身失败、或重取 200 但仍读不到 cookie → 一次即止+ `NetworkError('无法获取 CSRF 令牌')`。 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  return (await requestEnvelope<T>(config)).data
}

/** ⭐ 与 `request` **同一套**传输 / CSRF / 错误分类，额外把后端信封里的**`msg` 原样**交给调用方。为什么需要它：指派打手 / 抢单成功后，后端可能自动撤回代练通订单；**撤不掉时接口仍是成功码**，只在 `msg` 里带警告（「代练通订单未能撤回（原话：…），请先到『第三方平台订单管理』撤回…」）。只拿 `data` 的 `request()` 会把这句话**吞掉** ⇒ 前端必须能读到 `msg`。语义与 `request` 完全一致：`code !== 0` 抛 `BizError`（不返回失败信封）、传输失败抛`HttpError` / `NetworkError` / `AuthExpiredError`（同一段 catch 分类）。 */
export async function requestEnvelope<T>(config: AxiosRequestConfig): Promise<ApiResult<T>> {
  const method = (config.method ?? 'get').toLowerCase()
  const unsafe = UNSAFE_METHODS.has(method)
  // 「本次请求已经重取过 token」的标志。spec §4.3-3 要求标志挂在**这条请求**上、且不得是
  // 模块级永不复位的写法；这里是 `request()` 的局部变量 —— 请求结束即消失，天然复位。
  let csrfRefreshedThisRequest = false
  // 真正发出去的配置。CSRF 头只补在**副本**上（`withCsrfHeader`），调用方的 `config` 不被改。
  let finalConfig: AxiosRequestConfig = config

  try {
    if (unsafe) {
      const token = readCsrfCookie()
      if (token) {
        finalConfig = withCsrfHeader(config, token)
      } else {
        await refreshCsrfOrThrow() // 引导：先取 cookie 再发原请求（spec §4.3-2）
        csrfRefreshedThisRequest = true
        finalConfig = withCsrfHeader(config, readCsrfCookie() ?? '')
      }
    }
    return await sendEnvelope<T>(finalConfig)
  } catch (e) {
    if (e instanceof AxiosError && e.response) {
      const status = e.response.status
      const msg = envelopeMsg(e.response.data)
      if (!wasOrderFinanceDirect(e.config) && isCsrfFailure(status, msg)) {
        // 一次即止：本次请求已经引导重取过，就不再重取第二次（spec §4.3-3/4）。
        if (csrfRefreshedThisRequest) {
          throw new NetworkError(CSRF_UNAVAILABLE_MSG)
        }
        await refreshCsrfOrThrow()
        // 重取拿到了 200、却仍读不到 cookie（如 CSRF_COOKIE_HTTPONLY 被改回 True）：
        // 再发一次原请求也只会再吃一个 403，所以这里直接明确报错（spec §4.3-4）。
        if (!readCsrfCookie()) {
          throw new NetworkError(CSRF_UNAVAILABLE_MSG)
        }
        const err = new HttpError(403, '会话校验已刷新，请重试')
        err.csrfRefreshed = true
        throw err
      }
      // 401 分流（spec §5.4）：**默认 fail-closed**——只有显式传 `skipUnauthorizedHandler: true`
      // 的调用点（登录 / 引导 me / CSRF 重取，都在 auth.ts）才不触发处理器。
      if (status === 401 && !wasOrderFinanceDirect(e.config) && config.skipUnauthorizedHandler !== true) {
        onUnauthorized?.()
      }
      throw mapHttpStatus(status, msg)
    }
    if (e instanceof AxiosError) {
      throw new NetworkError(e.code === 'ECONNABORTED' ? '请求超时' : '网络异常，请检查连接')
    }
    throw e
  }
}

/** 成功路径：取出**整个信封**（`{code,msg,data}`）并先校验业务码。
 * 业务失败仍抛 `BizError`（`unwrap` 的唯一职责）——所以 `requestEnvelope` 的调用方
 * **只会**拿到 `code === 0` 的信封，`msg` 是成功语境下的原话（可能带警告）。 */
async function sendEnvelope<T>(config: AxiosRequestConfig): Promise<ApiResult<T>> {
  const res = await http.request<unknown>(config)
  if (!isEnvelope(res.data)) {
    // 响应到了，但不是协商好的形状——归入 NetworkError（它的文档语义已按此实现放宽，见 errors.ts）。
    throw new NetworkError('后端返回了非信封响应，无法解析')
  }
  const envelope = res.data as ApiResult<T>
  unwrap(envelope) // code !== 0 ⇒ 抛 BizError（与 `request` 的既有行为一致）
  return envelope
}
