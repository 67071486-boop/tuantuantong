/**
 * layui island 的公共设施（D-0025《前端 1:1 复刻标准》）。
 *
 * 每个 island 页都做同样三件事：**加载 layui → 注入对标覆盖样式 → 渲染**。
 * 前两件收在这里，页面模块只写第三件（渲染 + 交互）。
 *
 * ⚠️ 覆盖样式**必须在 layui.css 之后注入**：layui 的 CSS 是 `loadLayui()` 在运行时
 * `<link>` 进 head 的，静态写在 HTML 里的 `<style>` 会排在它前面、被同等特异性的规则盖掉。
 */
import { loadLayui, type LayuiNamespace } from './loader'
import { ensureBenchmarkVendor } from './vendor'

/**
 * 页面级覆盖：**只取对标物的「值」**，不提交他们的 `admin.css` / `zdy.css` / `layui.css` 文件本身。
 *
 * | 取值 | 来源（对标物） | 为什么我们的 vendored layui 不够 |
 * |---|---|---|
 * | `html{background:#f2f2f2;color:#666}` | `admin.css` 的 `html{…}`（**只有内容之下的空白**才是灰的） | — |
 * | `body{background:#fff;font-size:12px}` | 页面内联 `<style>` L25-27 的 `body{background-color:#fff}` ＋ `<body style="font-size:12px">` L43 | ⚠️ **唯一一处「出处是页内、却刻意留在全局」的值**（2026-09-20 裁决）：`background-color:#fff` 按判据本应搬回该页，但实测**每个查过的对标页页内 `<style>` 都自带同一句**（连没有页内 style 的 `/GameAccounts/importacc` 实测背景也是白），放全局不产生偏差；且 `parity.mjs` 比两侧 `body`/`html` 背景色，真分歧会被抓到。**其余页内规则一律搬回该页**（踩过：`.layui-table-cell` 当全局 → 无页内覆盖的页表头差 2px 且 parity 不查） |
 * | `body{font-family:"Helvetica Neue",Helvetica,"PingFang SC",Tahoma,Arial,sans-serif}` | 对标 `layui.css` 的 `body{…;font:14px Helvetica Neue,Helvetica,PingFang SC,Tahoma,Arial,sans-serif}` | 我们的 vendored `layui.css` 用**长写** `font-size`/`font-family`，字体栈是 `-apple-system,Roboto,…`；字体不同会让**每个汉字的 `-`/`/` 与空格宽度**都不同（实测：工具栏「禁用/启用」字号同 12px 但文本宽 64 vs 66，筛选栏每格累积 0.65px 漂移） |
 * | `body{line-height:normal}` | 同上：对标那条规则里的 **`font:` 简写会把 `line-height` 重置成 `normal`**（简写里没写 line-height） | 我们是长写，`line-height:1.6` 活了下来 → 全页继承 `19.2px`（=1.6×12）。这是差异 #2 的**唯一根因**：`probe-bench.json` 里 `form`/`.layui-table`/`.layui-table-header`/`tbody tr` 的 `line-height` 全是 `normal`，我们是 `19.2px`；分页更露骨——`.layui-laypage{font-size:0}` 让 `1.6` 算成 `0px` |
 * | ~~`.layui-table-cell{height:40px;line-height:40px;padding:0 5px;font-size:12px}`~~ | **页内局部**：`User__userlist.html` 的内联 `<style>` **L53-64**，**不是**全站值 | **已搬回**该页（`layui-island/pages/system-user-list.ts` 的页内 `<style>`）。留在全局会把这一页的局部值钉给所有页：实测无这条覆盖的对标页（如 `/System/AutomaticAdJustPriceSet`）表头高 **39 vs 我们 41**、整表 +2px——它们生效的是对标 `layuiadmin/layui/css/layui.css` 的 `.layui-table-cell{height:38px;line-height:28px;padding:6px 15px}`（与我们 vendored 的 `public/layui/css/layui.css` **逐字相同**） |
 * | 表格 `padding:6px 6px; line-height:20px; font-size:12px; border:1px solid #e2e2e2` | `zdy.css` L32-38 | — |
 * | `::-webkit-input-placeholder{color:#ccc}` | `admin.css` 的 `::-webkit-input-placeholder{color:#ccc}` | ⚠️ 漏了它时**所有带 `placeholder` 的输入框**都会是 Chrome 默认的 `rgb(117,117,117)` 深灰，对标是 `#ccc` 浅灰（实测两侧 `getComputedStyle(el,'::placeholder').color`）。影响全批 40+ 页，像素 diff 上是一眼可见的差异 |
 * | `.lableb`（角色职位那种小徽标：白字、圆角 3px、`padding:2px 4px`） | `zdy.css` L2-14 ＝ 页面内联 `<style>` L65-76 | — |
 * | 行 hover / 偶数行 `#abf8dd` | `zdy.css` L67-69 | — |
 * | `.layui-btn{line-height:38px}` / `-lg:44px` / `-sm:30px` / `-xs:22px` | 对标 `layui.css` 里这 4 条规则（该文件是压缩单行）：`.layui-btn{…height:38px;line-height:38px;…}`、`.layui-btn-lg{height:44px;line-height:44px;…}`、`.layui-btn-sm{height:30px;line-height:30px;…}`、`.layui-btn-xs{height:22px;line-height:22px;padding:0 5px;font-size:12px}` | 我们的 vendored `layui.css` 这 4 条把 `line-height` 各写了 `height - 2`（`36px`/`42px`/`28px`/`20px`）。**`line-height` 不是布局值却是可见值**：它决定文字在按钮里的垂直基线，也是差异 #3 与 #8 的根因。`height` 两侧相同，不要动 |
 * | `.layui-form-item/.layui-input-inline/.layui-form-pane/.layui-layer-content{overflow:visible!important}` | **页内局部**：`User__userlist.html` 的内联 `<style>` **L88-104**（原文是 **4 条独立规则**，不是一条并写） | **已搬回**该页（`system-user-list.ts` 的页内 `<style>`，同上）。它服务的是那一页自己的下拉/弹窗溢出，不是全站行为 |
 * | `.layui-card-body{position:relative;padding:10px 15px;line-height:24px}` | 对标 `layui.css` 的同名规则 | 我们的 vendored 版把它并成了 `.layui-card-body,.layui-card-header{position:relative;padding:10px 15px}`，**丢了 `line-height:24px`**，于是卡片内所有元素的行高回落到 body。人管页的接单统计卡片（`.recTotalDiv`）里 3 个 `.layui-input-inline` 实测由此差 24px → normal |
 * | `.layui-card-header{position:relative;height:42px;line-height:42px;padding:0 15px;…}` | 对标 `layui.css` 的同名规则 | 同上那条合并规则把 header 的 `padding` 写成了 10px 15px、且没有 `height/line-height`。本页没有 card header（**没有实测差异**），先按对标值放好，免得后续页面再踩 |
 * | `.layadmin-shortcut li{text-align:center}` / `li .layui-icon{…60px;font-size:30px;background-color:#F8F8F8…}` / `li cite{…display:block…}` / `li:hover .layui-icon{background-color:#f2f2f2}` | 对标 `admin.css`（`layuiadmin/style/admin.css`） | **漏了它整排就「小一号」**：`<i>` 与 `<cite>` 掉回 inline 排版、没有 60px 浅灰块。2026-09-24 业务方报「大小没有 1:1 复刻」的根因就是这几条没搬（`Index__console.html` 那一排只写了类名，样式全在这张全局表里） |
 * | `[lay-href],[lay-tips],[layadmin-event]{cursor:pointer}` | 同上 `admin.css` | 全站：带这三个属性的元素应为手型光标。漏了它们，复刻页上「看起来能点」的元素光标是箭头 |
 *
 * ⚠️ **本表只放对标「全局样式表」里的值**（`layui.css` / `admin.css` / `zdy.css`——每页都会加载的那三个）。
 * 判据（2026-09-20 收敛）：**来自某一页页内 `<style>` 的规则一律搬回那一页**，别放这里——
 * 放这里等于把那一页的局部值钉给所有页，而「页内没有这条覆盖」的对标页会立刻偏 2px（实测见上表两行）。
 * 已按这条判据搬回 `system-user-list.ts` 的有两条：`.layui-table-cell{…40/40/0 5px}` 与
 * 4 条 `…{overflow:visible!important}`。
 * 新页若发现某条不适用，先判它是「三个全局 CSS 里的」还是「某页页内的」，再决定放哪。
 *
 * ⚠️ 已知的**未搬运**差异（会影响后续页面，发现即来补）：对标 `layui.css` 的
 * `.layui-colla-icon{position:absolute;left:15px;top:0;font-size:14px}`、
 * `.layui-menu-body-title a{…}`（无 `overflow:hidden;text-overflow:ellipsis`）、
 * `.layui-laypage input`（无 `transition:none`）、
 * `.layui-card-body[pad15]{padding:15px}` / `[pad20]{padding:20px}`（我们整条缺失）。
 * 它们没进探针的选择器集，本页也没触发。
 */
export const ISLAND_OVERRIDES = `
html{background-color:#f2f2f2;color:#666}
body{background-color:#fff;font-size:12px;font-family:"Helvetica Neue",Helvetica,"PingFang SC",Tahoma,Arial,sans-serif;line-height:normal}
.layui-table td,.layui-table th{padding:6px 6px;min-height:20px;line-height:20px;border:1px solid #e2e2e2;font-size:12px}
::-webkit-input-placeholder{color:#ccc}
.lableb{display:inline-block;padding:2px 4px;font-size:12px;line-height:14px;color:#fff;white-space:nowrap;vertical-align:middle;background-color:#999;overflow:hidden;border-radius:3px;margin-right:2px}
.layui-table-click,.layui-table-hover,.layui-table[lay-even] tbody tr:nth-child(even){background-color:#abf8dd}
.layui-btn{line-height:38px}
.layui-btn-lg{line-height:44px}
.layui-btn-sm{line-height:30px}
.layui-btn-xs{line-height:22px}
.layui-card-body{position:relative;padding:10px 15px;line-height:24px}
.layui-card-header{position:relative;height:42px;line-height:42px;padding:0 15px;border-bottom:1px solid #f8f8f8;color:#333;border-radius:2px 2px 0 0;font-size:14px}
.layadmin-shortcut li{text-align:center}
.layadmin-shortcut li .layui-icon{display:inline-block;width:100%;height:60px;line-height:60px;text-align:center;border-radius:2px;font-size:30px;background-color:#F8F8F8;color:#333;transition:all .3s;-webkit-transition:all .3s}
.layadmin-shortcut li cite{position:relative;top:2px;display:block;color:#666;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;font-size:14px}
.layadmin-shortcut li:hover .layui-icon{background-color:#f2f2f2}
[lay-href],[lay-tips],[layadmin-event]{cursor:pointer}
`

/**
 * 对标每个页面都把基准字号写在 **`<body>` 的行内样式**上（`User__userlist.html` L43：
 * `<body style="font-size:12px;" id="iosiframe">`），不是写在 CSS 里。
 *
 * island 的外壳页 `layui-island/page.html` 是**全站共用一个** `<body>`（不在本页改动范围），
 * 所以在这里补上同一个值——探针的 `bodyInlineStyle` 就是比这个属性原文（实测：
 * 补之前是 `''` / 补之后 `font-size:12px;`），而 `bodyFontSize` 两边本来就都是 12px。
 * 只在 body **没有**行内样式时才写，不覆盖未来外壳自己加的东西。
 */
function applyBodyInlineStyle(doc: Document): void {
  if (doc.body && !doc.body.hasAttribute('style')) doc.body.setAttribute('style', 'font-size:12px;')
}

export function injectIslandOverrides(doc: Document = document): void {
  if (doc.getElementById('layui-island-overrides')) return
  const style = doc.createElement('style')
  style.id = 'layui-island-overrides'
  style.textContent = ISLAND_OVERRIDES
  doc.head.appendChild(style)
}

/** island 与父窗口（Vue 壳）之间的跳转请求。 */
export type IslandContext = {
  onNavigate: (name: string, params?: Record<string, string>) => void
}

/** 每个 island 页模块的出口形状（`layui-island/page.ts` 的注册表按它调用）。 */
export type IslandPage = {
  mount: (root: HTMLElement, ctx: IslandContext) => Promise<() => void>
}

/**
 * 加载 layui 模块 + 注入对标 vendor 资产 + 注入覆盖样式。页面模块第一行调它。
 *
 * 顺序（每一步都有理由，别调换）：
 * 1. `loadLayui()` → 插 `layui.css` + `layui.js`；
 * 2. `ensureBenchmarkVendor()` → 插 `vendor/*.css`（**必须在 layui.css 之后**，两者同特异性时后者胜）
 *    并对标那份 `formSelects-v4.js`（它的 UMD 要求 `window.layui` 已在，所以只能排在这里）；
 * 3. `injectIslandOverrides()` → 覆盖 `<style>` 排最后（原因见本文件顶部注释）。
 *
 * ⚠️ 代价（有意的）：每个 island 页都会带上 formselects 的 18.6 KB CSS + 60.2 KB JS，
 * 即使那页没有 `xm-select`。收益是「页面模块不必各自记得引资产」、且与对标加载顺序一致；
 * 若将来只有个别页需要，把这一句挪进那些页的 `mount` 里即可（`ensureBenchmarkVendor` 幂等）。
 */
export async function bootstrapIsland(modules: string[]): Promise<LayuiNamespace> {
  // `element` 一律随其它模块一起加载：对标页内的**页签 / 折叠面板**是纯客户端结构行为
  // （点页签切换面板），不是「功能」。不加载它的话页签点了没反应、第二个面板永远看不到——
  // 全站有 9 个 replica 页、13 个活页签块会一起「看起来坏了」。
  // 计数由 `artifact-tab-title.spec.ts` 的语料自证钉住。
  const layui = await loadLayui([...modules, 'element'])
  await ensureBenchmarkVendor()
  injectIslandOverrides()
  applyBodyInlineStyle(document)
  return layui
}

/** `islandTabTarget()` 命中时返回的三段 DOM（页签项 / 标题容器 / 面板容器）。 */
export type IslandTabTarget = { li: HTMLElement; title: HTMLElement; content: HTMLElement }

/**
 * 页签点击的**唯一判据**：「这一次点击会被 `initIslandElements` 接管吗」。
 *
 * 接管 = ① 点击落在 `.layui-tab-title > li` 上 **且** ② 它所属的 `.layui-tab` 直接带
 * `.layui-tab-content`、且该面板容器里**有** `.layui-tab-item`（= 确实有可切的面板）。
 * 满足 ⇒ 返回三段 DOM；不满足 ⇒ `null`（这次点击不归我们管，交给页面自己的处理器或
 * `artifactPage` 的 catch-all）。
 *
 * ⚠️ **两个调用方，一份判据（防漂移）**：
 *  - `initIslandElements()`（本文件）：接管时切标题 `layui-this` / 面板 `layui-show`；
 *  - `artifactPage.ts` 的 catch-all：接管时**不**弹「尚未开放」（页签是纯客户端结构行为，不是功能）。
 *  ⛔ 不许在任何一处另写一份等价判据。上一版 `artifactPage` 用的是**全局**排除
 *  `target.closest('.layui-tab-title')`，把「证据件只有 `.layui-tab-title`、**没有**面板容器」
 *  那类页面的提示也一起吞掉了——那种页签本就不被接管，「尚未开放」是它**唯一**的反馈
 *  （2026-09-22 实测，见 `artifact-tab-title.spec.ts` 的两类断言）。
 */
export function islandTabTarget(target: EventTarget | null): IslandTabTarget | null {
  const li = (target as HTMLElement | null)?.closest?.('.layui-tab-title > li') as HTMLElement | null
  if (!li) return null
  const title = li.parentElement
  const content = (title?.parentElement?.querySelector(':scope > .layui-tab-content') ??
    null) as HTMLElement | null
  if (!title || !content) return null
  // 「可切的面板」还必须有**面板项本身**：只有空容器时索引必然越界（`items.length === 0`），
  // 点了什么都看不到 ⇒ 这种页签不算被接管（与下面 `initIslandElements` 的 fail-safe 同一根因）。
  if (content.querySelector(':scope > .layui-tab-item') === null) return null
  return { li, title, content }
}

/**
 * 绑定页内**结构型**组件（页签 / 折叠面板）。**必须在页面标记注入之后调用**。
 *
 * 踩过（实测）：把 `element.init()` 放在 `bootstrapIsland()` 里是无效的——那一刻渲染器
 * 还没写 `root.innerHTML`，DOM 里没有任何 `.layui-tab`，layui 绑了个空；症状是
 * 「点页签不切换、`layui-this` 也不动」，9 个带活页签的 replica 页会一起看起来坏了。
 * 移到注入之后仍然不生效：这个 layui 构建的 `element.init()` / `element.render('tab')`
 * 都不接管 `.layui-tab` 的点击（实测：`tabChange('', 2)` 还会因为 `lay-filter` 为空抛
 * `Syntax error, unrecognized expression: .layui-tab[lay-filter=]`）。
 *
 * `element.init()` / `element.render('tab')` 不接管点击，但 tab 模块工厂在 document 上
 * 有冒泡委托（点 `.layui-tab-title li`）。仍由我们自己切类名：`user-mymessage` 的回调
 * 挂在捕获阶段、且在本函数之后注册，它读的是此刻已经切好的 `layui-this`。
 *
 * ⛔ **不再 `preventDefault()` / `stopPropagation()`**（上一版两句都在，那是
 * `/Emp/emplist`「销售客服提成账单」切不动的根因）：
 *  1. 上一版停掉冒泡的理由是「防止与 layui 自己的委托叠加成开了又关」——读码看**不成立**：
 *     `o.tabClick` 做的是 `addClass(CLASS_THIS)` + `siblings().removeClass(...)`、面板用
 *     `addClass(CLASS_SHOW)` + `siblings().removeClass(...)`，**是设置不是 toggle**，与我们的
 *     两次切换结果相同（幂等）；`CLASS_THIS`/`CLASS_SHOW` 也不是空值——它们由
 *     `layui.component` 的默认 `CONST` 注入（`CLASS_THIS:"layui-this"`,
 *     `CLASS_SHOW:"layui-show"`）。
 *  2. 更关键：**面板数少于标题数时它同样不藏面板**——`tabClick` 找不到带该 `lay-id` 的面板时
 *     回落到 `t.eq(idx)`，idx 越界就是**空集**，`addClass` / `siblings()` 都落在空集上。
 *     这条独立佐证了下面的 fail-safe 与「对标实测不藏」一致。
 *  3. 而 `stopPropagation()` 顺带把**页面自己的**冒泡监听（如
 *     `emp-emplist.ts::onTabClick`）一并挡掉了：`/Emp/emplist` 的 `.layui-tab-content` 里
 *     **只有 1 个面板**（两个页签共用一套 DOM），点第 2 个页签必然 `idx >= items.length`，
 *     上一版在 `return` **之前**就停掉了冒泡 ⇒ 页面永远收不到那次点击（零请求、类名也不动）。
 *  ⇒ 现在的口径：**只做我们确定要做的事**（标题无条件切、面板仅索引命中时切），其余一律不拦
 *  ——凡是我们没真正处理的点击，页面自己的处理器照常收到。
 */
export function initIslandElements(layui: LayuiNamespace): void {
  const element = (layui as Record<string, unknown>).element as
    | { init?: () => void }
    | undefined
  element?.init?.()

  const doc = document
  if (doc.documentElement.dataset.islandTabsBound === '1') return
  doc.documentElement.dataset.islandTabsBound = '1'
  doc.addEventListener(
    'click',
    (event: Event) => {
      // 判据**唯一出处** = `islandTabTarget`（`artifactPage` 的 catch-all 调的是同一个函数）；
      // 「点击在 `.layui-tab-title > li` 上、且该 tab 有 `content`、且 content 里有 `item`」。
      const tab = islandTabTarget(event.target)
      if (!tab) return
      const { li, title, content } = tab
      const idx = Array.prototype.indexOf.call(title.children, li)
      const items = Array.from(content.children)
      // ① 标题高亮：点谁谁 `layui-this`（= layui 的可见行为，无条件下切）。
      for (const sib of Array.from(title.children)) sib.classList.remove('layui-this')
      li.classList.add('layui-this')
      // ② 面板**只在索引对得上时**切。⚠️ **fail-safe**：面板数可能少于标题数（对标容忍这种
      //    写法：`/Emp/emplist` 就是「2 标题 / 1 面板」），按索引 `toggle` 会把**唯一的
      //    面板也藏掉**（整块内容消失），而对标实测**不藏**。索引落空时**什么都不做**
      //    （宁可保持现状，也不要清空页面）。由站内通知线在 `user-mymessage`
      //    （侧边 tab 3 标题 / 1 面板）实测发现并留了证据。
      if (idx < 0 || idx >= items.length) return
      items.forEach((item, i) => {
        item.classList.toggle('layui-show', i === idx)
      })
    },
    true,
  )
}

/** `layer.msg` 的薄封装（对齐标「尚未开放」提示的统一样式）。 */
export function islandMsg(layer: Record<string, unknown>, text: string): void {
  ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
}
