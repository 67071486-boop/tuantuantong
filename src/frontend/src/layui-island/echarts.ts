/**
 * ECharts 的**按需加载器**（经营报表页 `pages/emp-dayoperation.ts` 用）。
 *
 * ## 为什么是「vendored 静态文件」而不是 npm 依赖
 *
 * 业务方 2026-10-17 裁定（spec §0 第 2 条 / §5 末尾）：**引入 ECharts 静态文件**——
 * 把对标那份 `echarts.min.js` 原样放进 `public/vendor/`，页面按需加载，
 * **不加 npm 构建依赖**。这样做的代价如实登记在 spec §9 第 6 条：
 * 版本跟随对标离线产物、不参与前端构建、不锁 npm 版本；升级要手工换文件并回归两页图表。
 * ⛔ 别「顺手」改成 `npm i echarts`：那是一次新裁定，会动构建产物与 lockfile。
 *
 * ## 注入手法与 `layui/vendor.ts` 同源
 *
 * 「往 body 插 `<script>` + 用 `id` 去重 + 模块级 Promise 缓存」。`id` 去重是
 * **幂等**的判据：同一页里反复 `await loadECharts()` 只会插一次脚本。
 * 路径用 `import.meta.env.BASE_URL` 前缀（与 `vendor.ts` 同一口径）——写死 `/vendor/…`
 * 会让挂在子路径下的产物（`base` 不是 `/`）404。
 *
 * ## 失败形态（「不白屏」）
 *
 * 加载失败（404 / 断网 / 脚本执行报错）一律 `reject` 一句**中文人话**，
 * 由页面 `layer.msg` 显示。**不吞异常、不 resolve 一个空壳**——那样页面会在
 * `echarts.init` 处抛 `undefined is not a function`，用户只看到控制台里的英文栈。
 *
 * ⚠️ 失败**是粘性的**（模块级 Promise 不复位）：本页的生命周期里重试只会立刻拿到同一条
 * 中文提示。这不是省事——「失败后复位再插一次」会踩一个更难查的坑：上一次那个
 * `<script>` 节点头还挂在 DOM 里（`load` / `error` 都已触发过），复位后新建的 Promise
 * 只会给它**再挂一对**监听器，而它永远不会再触发 ⇒ **永久挂住**（不 resolve 也不 reject）。
 * 测试要复位用 `__resetForTest()`（它连同脚本节点一起清掉）。
 *
 * ⚠️ jsdom 里**不加载真脚本**（629 KB、且 jsdom 没 canvas）：页测试一律
 * `vi.mock('@/layui-island/echarts')`，本模块自己由 `tsc` 与真浏览器回归兜着。
 */
const BASE = import.meta.env.BASE_URL || '/'

/** 注入的 `<script>` 的 `id`（去重与清理都用它）。 */
export const ECHARTS_SCRIPT_ID = 'vendor-echarts'

/** 脚本地址（`BASE_URL` + `vendor/echarts.min.js`，文件在 `public/vendor/`）。 */
export const ECHARTS_SCRIPT_SRC = `${BASE}vendor/echarts.min.js`

/** ECharts 实例上本页用到的方法（只声明用到的，⛔ 不抄整份 d.ts）。 */
export type EChartsInstance = {
  /** 第二个参数 `true` = **不合并**旧配置（换一份数据时不会残留上一次的系列）。 */
  setOption: (option: unknown, notMerge?: boolean) => void
  resize?: () => void
  dispose?: () => void
}

/** `window.echarts` 上本页用到的方法。 */
export type EChartsNamespace = {
  init: (element: HTMLElement) => EChartsInstance
}

/** 脚本加载失败时的**唯一**中文文案（页面直接 `layer.msg` 它）。 */
export const ECHARTS_LOAD_ERROR = '图表库加载失败，请刷新页面重试'

/** 单例：同一页里只插一次脚本、只解析一次。 */
let promise: Promise<EChartsNamespace> | null = null

/**
 * 加载 ECharts，resolve `window.echarts`。
 *
 * ⚠️ 必须在**页面标记已进 DOM 之后**调用（`echarts.init` 要量容器尺寸；容器不在 DOM 里
 * 时宽高都是 0，图会画成一条线）。
 */
export function loadECharts(): Promise<EChartsNamespace> {
  if (promise) return promise
  promise = new Promise<EChartsNamespace>((resolve, reject) => {
    const existing = document.getElementById(ECHARTS_SCRIPT_ID) as HTMLScriptElement | null
    const script = existing ?? document.createElement('script')
    script.id = ECHARTS_SCRIPT_ID
    script.addEventListener('load', () => {
      const echarts = (window as { echarts?: EChartsNamespace }).echarts
      // 脚本 200 但没有挂上 `window.echarts`（被别的东西覆盖 / 文件被换成了别的内容）：
      // 同样按失败处理，⛔ 不 resolve 一个 `undefined` 让页面在 init 处崩。
      if (echarts === undefined || typeof echarts.init !== 'function') {
        reject(new Error(ECHARTS_LOAD_ERROR))
        return
      }
      resolve(echarts)
    })
    script.addEventListener('error', () => reject(new Error(ECHARTS_LOAD_ERROR)))
    if (!existing) {
      script.src = ECHARTS_SCRIPT_SRC
      document.body.appendChild(script)
    }
  })
  return promise
}

/**
 * **只给测试用**：清掉单例与脚本节点，让下一次 `loadECharts()` 重新插脚本。
 *
 * ⛔ 生产代码里不要调它。清节点是必须的——只清 `promise` 会把上面「失败粘性」注释里
 * 那个「给已 settle 的节点再挂监听器 ⇒ 永久挂住」的坑重新打开。
 */
export function __resetForTest(): void {
  promise = null
  document.getElementById(ECHARTS_SCRIPT_ID)?.remove()
}
