/**
 * 对标第三方资产的**可选**注入口（D-0025 前端 1:1 复刻标准的配套件）。
 *
 * 这里只放「对标页确实加载、而我们没有」的那两样：
 *
 * 1. **formselects-v4（xm-select）**——对标筛选栏的 3 个多选下拉（`选择职位`/`业务类型`/`成员类型`）
 *    靠它渲染出 `.xm-select-parent`（含「全选/清空/反选/没有选项」）。对标 `/User/userlist`
 *    第 16 行 `<link ... formselects-v4.css>`，运行时再取 `/layuiadmin//lib/formSelects-v4.js`
 *    （实测 `performance.getEntriesByType('resource')`）。两份文件已**原样**搬到
 *    `public/vendor/`，字体也一并落地（出处与字节数见 `formselects-v4.css` 的文件头注释）。
 *
 * 2. **对标「私有图标类」的审计结论**——`layui-zhuce`/`layui-biaoji1`/… 这些类**没有**字体、
 *    也**没有**任何 CSS 规则（实测 `::before content:none`、宽 4px＝`.layui-btn .layui-icon{padding:0 2px}`），
 *    所以没有可搬的资产，结论与证据全部写在 `public/vendor/benchmark-icons.css`（纯注释、零规则）。
 *
 * 注入手法**复用 `./loader`**：同样是「往当前文档 head 插 `<link>` / 往 body 插 `<script>`」、
 * 用 `id` 去重、用模块级 Promise 缓存，不另起一套。所有函数都跑在 island 的 iframe 文档里
 * （`document` 就是 iframe 的 document），因此不影响父窗口的 Vue 壳。
 *
 * ⚠️ 与对标的一个已知顺序差异：对标是 `layui.config({base}).extend({formSelects:'/lib/formSelects-v4'})`
 * 由 layui 去取文件；我们是先把文件原样插进来（此时 `window.layui` 已在，UMD 会走
 * `layui.define(['jquery'], …)` 分支注册成 layui 模块），再 `layui.use(['formSelects'])` 认领。
 * 实测两者拿到的 `layui.formSelects` 是同一个构造器（见 `initFormSelects` 的用法）。
 */
import { loadLayui, type LayuiNamespace } from './loader'

const BASE = import.meta.env.BASE_URL || '/'

/** formselects v4 的公开实例（只声明我们用到的方法）。 */
export type FormSelectsInstance = {
  /** `render(id?)`：给 `id` 时只渲染 `select[xm-select="id"]`，不带参数渲染全部。 */
  render: (id?: string | Record<string, unknown>, options?: Record<string, unknown>) => unknown
  /** `value(id, 'val'|'valStr'|'name'|'nameStr')`：读当前值；`value(id, [])`：清空已选。 */
  value: (id: string, type?: string | unknown[], isAppend?: boolean) => unknown
}

type LayuiWithFormSelects = LayuiNamespace & { formSelects?: FormSelectsInstance }

function injectCss(id: string, href: string): void {
  if (document.getElementById(id)) return
  const link = document.createElement('link')
  link.id = id
  link.rel = 'stylesheet'
  link.href = href
  document.head.appendChild(link)
}

let scriptPromise: Promise<void> | null = null

function injectFormSelectsScript(): Promise<void> {
  if (scriptPromise) return scriptPromise
  scriptPromise = new Promise<void>((resolve, reject) => {
    const id = 'benchmark-formselects-js'
    const existing = document.getElementById(id) as HTMLScriptElement | null
    const script = existing ?? document.createElement('script')
    script.id = id
    script.addEventListener('load', () => resolve())
    script.addEventListener('error', () => reject(new Error('formSelects-v4 脚本加载失败')))
    if (!existing) {
      script.src = `${BASE}vendor/formselects-v4.js`
      document.body.appendChild(script)
    }
  })
  return scriptPromise
}

let vendorPromise: Promise<void> | null = null

/**
 * 注入对标 vendor 资产（**幂等**，重复调用只插一次）。
 *
 * 时机：由 `island.ts` 的 `bootstrapIsland()` 在 `loadLayui()` **之后**调用，所以
 * `window.layui` 必然已在（这正是 formSelects 的 UMD 走 layui 分支的前提）。
 * 独立调用时本函数自己先 `loadLayui(['form'])` 把 `layui.js` 备好——`layui.use` 对已加载的
 * 模块是缓存命中，不会重复下载。
 *
 * ⚠️ 顺序对**渲染**有影响：formSelects 的构造器（`new Select4()`）一执行就会 `render()` 扫一遍
 * 当时的 `select[xm-select]`。本函数在页面写 DOM **之前**跑，所以那时扫不到东西是正常的，
 * 真正的渲染由页面在插完 DOM 后调 `initFormSelects()` 完成（对标也是 `userlist.js` 里再补）。
 */
export function ensureBenchmarkVendor(): Promise<void> {
  if (vendorPromise) return vendorPromise
  injectCss('benchmark-icons-css', `${BASE}vendor/benchmark-icons.css`)
  injectCss('benchmark-formselects-css', `${BASE}vendor/formselects-v4.css`)
  vendorPromise = (async () => {
    // 1) 先确保 layui.js 在（否则下面的 UMD 会走 `window.formSelects = factory()`，
    //    拿不到 `layui.formSelects`，与页面的 layui 生态脱节）。
    await loadLayui(['form'])
    // 2) 原样插入对标那份 formSelects-v4.js。
    await injectFormSelectsScript()
    // 3) 让它以 layui 模块身份被认领（`layui.define` 已在脚本里注册好，这里是缓存命中）。
    await loadLayui(['formSelects'])
  })()
  return vendorPromise
}

/** 取已就绪的 `layui.formSelects`；还没注入时返回 `undefined`（不抛，调用方决定要不要报错）。 */
export function getFormSelects(): FormSelectsInstance | undefined {
  return (window.layui as LayuiWithFormSelects | undefined)?.formSelects
}

/**
 * 对指定 `<select>` 调 `xmSelect.render`。**先 `await ensureBenchmarkVendor()`**（或走
 * `bootstrapIsland()`，它已经调用过），否则这里会拿不到实例。
 *
 * - `selector`：CSS 选择器，元素必须带 `xm-select="<id>"` 属性（对标就是这个约定，
 *   见 `User__userlist.html` L142/160/171）。渲染参数（`xm-select-radio`/`-height`/`-skin`）
 *   也从这些属性读，不需要另传。
 * - `options`：可选的 formSelects 配置；**为空时不传第二个参数**——formSelects 的 `render(id, {})`
 *   会用 `undefined` 覆盖内部默认配置（源码 L1627-1650 的 `$.extend(fsConfigs[id], config)`），
 *   有实际风险，所以只在真有值时透传。
 * - **幂等**：同一个 `id` 已渲染出 `dl[xid="…"]` 就跳过，重复调用不会叠出第二个 `.xm-select-parent`。
 */
export function initFormSelects(selector: string, options: Record<string, unknown> = {}): void {
  const fs = getFormSelects()
  if (!fs) {
    console.warn('[layui/vendor] formSelects 尚未就绪：请先 await ensureBenchmarkVendor()')
    return
  }
  const selects = Array.from(document.querySelectorAll<HTMLSelectElement>(selector))
  const seen = new Set<string>()
  for (const el of selects) {
    const id = el.getAttribute('xm-select')
    if (!id || seen.has(id)) continue
    seen.add(id)
    if (document.querySelector(`dl[xid="${id}"]`)) continue
    if (Object.keys(options).length > 0) fs.render(id, options)
    else fs.render(id)
  }
}
