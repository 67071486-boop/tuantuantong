import { installOrderFinanceTableTransport } from './orderFinanceTransport'

/**
 * 按需加载 **vendored 的官方 layui 2.13.9**（MIT，在 `public/layui/`）。
 *
 * 为什么是 vendored 而不是 npm 依赖：npm 的 `layui` 包**只有核心**（`layui.js` 里没有
 * `modules/`，`layui.use(['table'])` 会去 `modules/table.js` 取文件而 404）；
 * 官方发行版的 `layui.js` 是 **all-in-one**（30 个 `layui.define`，table/form/laydate/layer 都在），
 * 但它必须用 `<script src>` 加载（layui 靠 `document.currentScript` 推导自身目录），
 * 所以只能放静态目录、由这里动态插标签。
 *
 * ⚠️ 这套东西是**全局前端标准**（D-0025）：**租户后台的所有页面都按对标物像素级 1:1 复刻**，
 * 都用这种 island。Element Plus 只留给**壳**（顶栏/侧栏/页签条）与**尚未改造的页面**，
 * 两套并存是**有意的**。做法、取值纪律与自检清单见 `docs/FRONTEND-REPLICA-STANDARD.md`。
 */
export type LayuiNamespace = {
  use: (modules: string | string[], callback: () => void) => void
  form: LayuiModule
  table: LayuiModule
  layer: LayuiModule
  laydate: LayuiModule
  dropdown: LayuiModule
  laytpl: LayuiModule
  util: LayuiModule
  $: unknown
}

/** layui 模块的 API 是运行时动态的，这里只当作「有方法的对象」用。 */
export type LayuiModule = Record<string, unknown> & ((...args: unknown[]) => unknown)

declare global {
  interface Window {
    layui?: LayuiNamespace
  }
}

const BASE = import.meta.env.BASE_URL || '/'

function injectCss(): void {
  const id = 'layui-css'
  if (document.getElementById(id)) return
  const link = document.createElement('link')
  link.id = id
  link.rel = 'stylesheet'
  link.href = `${BASE}layui/css/layui.css`
  document.head.appendChild(link)
}

let scriptPromise: Promise<void> | null = null

function injectScript(): Promise<void> {
  if (scriptPromise) return scriptPromise
  scriptPromise = new Promise<void>((resolve, reject) => {
    if (window.layui) {
      resolve()
      return
    }
    const id = 'layui-js'
    const existing = document.getElementById(id) as HTMLScriptElement | null
    const script = existing ?? document.createElement('script')
    script.id = id
    script.addEventListener('load', () => resolve())
    script.addEventListener('error', () => reject(new Error('layui 脚本加载失败')))
    if (!existing) {
      script.src = `${BASE}layui/layui.js`
      document.body.appendChild(script)
    }
  })
  return scriptPromise
}

/**
 * 加载并 `use` 指定模块；返回**同一个** layui 命名空间（`this`）。
 * 重复调用只插一次脚本，模块由 layui 自己缓存。
 */
export function loadLayui(modules: string[]): Promise<LayuiNamespace> {
  injectCss()
  return injectScript().then(
    () =>
      new Promise<LayuiNamespace>((resolve) => {
        window.layui!.use(modules, function (this: LayuiNamespace) {
          installOrderFinanceTableTransport(this.$)
          resolve(this)
        })
      }),
  )
}
