/**
 * 网页端 Chatwoot 咨询气泡（桌面后台专用）。计划：`docs/plans/2026-10-22-web-chatwoot-widget.md` §2.1/§2.3。
 * 桌面根组件 `App.vue` 管理两套后台壳的挂载与清理。
 * SDK 硬闸（读自 sdk.js）：首次 setUser 必须等 `chatwoot:ready`（就绪前 setUser 写死 cookie 却丢 postMessage，ready 补发被按同值跳过）；登出先 hide 再 reset、各自 try/catch（顺序反了会留下可点气泡）；reset 后 ready 不再触发 ⇒ 换人同步要立即发。配置单一真值源 = 后端 `user` 载荷四字段（⛔ 无 `VITE_CHATWOOT_*` 兜底），缺任一项 ⇒ 不注入、不同步（fail-closed，不退化匿名）。
 */
import type { User } from '@/api/auth'

/** 已注入的 `<script>` 用这个属性认（HMR / 模块被重复求值时也认得出来）。 */
const SCRIPT_ATTR = 'data-chatwoot-widget'

/** Chatwoot 的 locale 值形如 `zh_CN`（⛔ 不是 BCP 47 的 `zh-CN`；计划 §4 已钉）。 */
const LOCALE = 'zh_CN'

type BubbleVisibility = 'show' | 'hide'

/** `$chatwoot` 里本模块用到的那几个方法（其余不声明，避免假装知道整个 SDK 契约）。 */
export interface ChatwootWidgetApi {
  setUser?: (
    identifier: string,
    attributes: {
      name?: string
      email?: string
      avatar_url?: string
      identifier_hash?: string
    },
  ) => void
  toggleBubbleVisibility?: (visibility: BubbleVisibility) => void
  reset?: () => void
}

declare global {
  interface Window {
    chatwootSettings?: Record<string, unknown>
    chatwootSDK?: {
      run: (options: { websiteToken: string; baseUrl: string }) => void
    }
    $chatwoot?: ChatwootWidgetApi
    /**
     * 当前模块实例注册的 `chatwoot:ready` 处理器（跨模块实例 / HMR 去重用）。
     * ⚠️ 不写进这层它就只是个私有变量：HMR 重新求值模块时旧监听器留在 `window` 上，
     * 两个实例会各同步一次（最坏是拿陈旧的 `pendingUser` 去 `reset` 新会话）。
     */
    __chatwootReadyHandler?: () => void
  }
}

/** 是否已经注入过（一个标签页只注入一次；`/` 与 `/kefu` 的壳不会各来一份）。 */
let injected = false
/** 待同步的用户。SDK 就绪晚于 `setUser` 的时机时，靠 `chatwoot:ready` 补一次。 */
let pendingUser: User | null = null
/** `chatwoot:ready` 监听器只绑一次（一个标签页）。 */
let readyBound = false
/**
 * SDK 的 iframe 是否已就绪（`chatwoot:ready` 已到）。⚠️ 承重闸：就绪前调 `setUser` 会写死 cookie 却丢 postMessage，
 * 之后 ready 补发被 SDK 按同值跳过（见模块 docstring）；reset 后 ready 不再触发 ⇒ 那时本闸已 true，换人同步立即发。
 */
let sdkReady = false

/**
 * 从 `user` 载荷读 SDK 的两个公开配置；缺任一项 ⇒ `null`（不注入，fail-closed）。
 * 值与后端 `CHATWOOT_WEB_*` 是同一份真值源（后端下发），⛔ 不加 `import.meta.env` 兜底——那会变成两份配置。
 */
function readConfig(user: User | null): { baseUrl: string; websiteToken: string } | null {
  const baseUrl = (user?.chat_base_url ?? '').trim().replace(/\/+$/, '')
  const websiteToken = (user?.chat_website_token ?? '').trim()
  if (baseUrl === '' || websiteToken === '') return null
  return { baseUrl, websiteToken }
}

function chatwootApi(): ChatwootWidgetApi | undefined {
  if (typeof window === 'undefined') return undefined
  return window.$chatwoot
}

/** 只调方法、吞掉 SDK 的异常：登出路径（`clear()`）绝不能被气泡带崩。 */
function callQuietly(action: () => void): void {
  try {
    action()
  } catch {
    // 刻意留空：Chatwoot 的节点可能还没建好；隐藏 / reset 失败都不该影响会话终结。
  }
}

function hideBubbleOnly(): void {
  const api = chatwootApi()
  if (!api?.toggleBubbleVisibility) return
  callQuietly(() => api.toggleBubbleVisibility?.('hide'))
}

/**
 * 隐藏气泡 + 清会话。顺序承重（见模块 docstring）：先 hide、后 reset，各自 try/catch（reset 抛错时隐藏已发生，调用方不崩）。
 * ⚠️ 只在 `sdkReady` 之后调（见 `settleWithoutUser`）：就绪前 reset 会让 `chatwoot:ready` 永不触发、本标签页再也同步不了。
 */
function hideAndReset(): void {
  hideBubbleOnly()
  const api = chatwootApi()
  if (!api?.reset) return
  callQuietly(() => api.reset?.())
}

/**
 * 「没有用户」的落点：就绪前只隐藏，就绪后才连会话一起 reset（ready 到点时会再走一次本函数，那时才 reset）。
 * ⚠️ 就绪前 reset 是全局代价：SDK 置位后不再发 `chatwoot:ready` ⇒ 换人登录时 `setUser` 被闸挡住、身份永远同步不上。
 */
function settleWithoutUser(): void {
  if (!sdkReady) {
    hideBubbleOnly()
    return
  }
  hideAndReset()
}

function showBubble(): void {
  const api = chatwootApi()
  if (!api?.toggleBubbleVisibility) return
  callQuietly(() => api.toggleBubbleVisibility?.('show'))
}

/** 按 `pendingUser` 落地：有身份 ⇒ setUser + show；没有 ⇒ 隐藏并清会话。 */
function applyPendingUser(): void {
  const api = chatwootApi()
  if (!api) return // SDK 还没注入：等 `chatwoot:ready`。

  const user = pendingUser
  const identifier = user?.chat_identifier
  const identifierHash = user?.chat_identifier_hash
  if (!user || !identifier || !identifierHash) {
    // 「没身份」与「有身份但后端没下发签名」同一条路：不 setUser（⛔ 不退化成匿名）。
    settleWithoutUser()
    return
  }
  // ⚠️ 首次同步必须等 iframe 就绪（模块 docstring 事实 4）；reset 之后这个闸早已打开。
  if (!sdkReady) return

  if (api.setUser) {
    const attributes: {
      name: string
      identifier_hash: string
      avatar_url?: string
    } = {
      // 计划 §2.1：显示名空时回落「用户<id>」——SDK 要求 name / email / avatar_url 至少一个。
      name: user.display_name || `用户${user.id}`,
      identifier_hash: identifierHash,
    }
    if (user.avatar_url) attributes.avatar_url = user.avatar_url
    callQuietly(() => api.setUser?.(identifier, attributes))
  }
  // reset 之后 `chatwoot:ready` 不再触发 ⇒ 换人登录必须在这里立即 show（计划 §2.1）。
  showBubble()
}

function onChatwootReady(): void {
  sdkReady = true
  applyPendingUser()
}

function bindReadyListener(): void {
  if (readyBound || typeof window === 'undefined') return
  readyBound = true
  // 先摘掉上一个模块实例的监听器（HMR / 测试里 resetModules 都会留下一条）。
  const previous = window.__chatwootReadyHandler
  if (previous) window.removeEventListener('chatwoot:ready', previous)
  window.__chatwootReadyHandler = onChatwootReady
  window.addEventListener('chatwoot:ready', onChatwootReady)
}

/**
 * 动态注入 Chatwoot SDK 并 `run()`，幂等（重复调用只注入一次）。返回 `true` = 已挂载（含之前已注入）；
 * `false` = 四个字段缺任一个、没有注入（调用方据此跳过 `syncChatwootUser`，fail-closed）。刻意不写进 `index.html`：登录页不该加载它（计划 §2.1）。
 */
export function mountChatwoot(user: User | null): boolean {
  if (typeof window === 'undefined' || typeof document === 'undefined') return false
  if (injected || window.$chatwoot) return true
  if (document.querySelector(`script[${SCRIPT_ATTR}]`)) {
    // 别的模块实例已经注入（HMR）：认它，别再来一份。
    injected = true
    return true
  }

  const config = readConfig(user)
  if (!config) return false

  window.chatwootSettings = {
    locale: LOCALE,
    position: 'right',
    launcherTitle: '联系客服',
  }
  bindReadyListener()
  injected = true

  const script = document.createElement('script')
  script.src = `${config.baseUrl}/packs/js/sdk.js`
  script.async = true
  script.defer = true
  script.setAttribute(SCRIPT_ATTR, 'true')
  script.onload = () => {
    // `run` 由刚加载的 sdk.js 定义；调用后 `$chatwoot` 同步存在（见模块 docstring 事实 1）。
    callQuietly(() => {
      window.chatwootSDK?.run({
        websiteToken: config.websiteToken,
        baseUrl: config.baseUrl,
      })
    })
    // ⚠️ 这里**不**调 `applyPendingUser()`：此刻 iframe 还没 load 完，`setUser` 会写死
    // cookie 却丢消息，把之后的 ready 补发也变成 no-op（模块 docstring 事实 4）。
  }
  script.onerror = () => {
    // 加载失败 ⇒ 允许下一次挂载重试（否则一次网络抖动就把本标签页永久废掉）。
    injected = false
  }
  document.head.appendChild(script)
  return true
}

/**
 * 同步当前用户：有 `chat_identifier` + `chat_identifier_hash` ⇒ `setUser` + `show`；
 * `null`（登出 / 401）⇒ 先隐藏、再 reset（顺序见模块 docstring）。⚠️ `$chatwoot` 不存在时什么都不做、一律不抛：`stores/session.ts` 的 `clear()` 是同步路径，不能被这里带崩。
 */
export function syncChatwootUser(user: User | null): void {
  pendingUser =
    user && user.chat_identifier && user.chat_identifier_hash ? user : null
  applyPendingUser()
}

/** 登出 / 卸载：隐藏气泡并清掉 Chatwoot 会话 cookie（顺序与异常语义见 `hideAndReset`）。 */
export function unmountChatwoot(): void {
  pendingUser = null
  settleWithoutUser()
}
