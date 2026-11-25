/**
 * **证据件复刻页**（D-0025 的通用渲染器之二）：把对标页的**原始标记**搬进来渲染。
 *
 * 与 `replicaPage.ts`（声明式拼装列表页）的分工：
 *  - 形状是「筛选 + 工具栏 + 表格」且结构简单 → `replicaPage(spec)`
 *  - 形状是页签 / 卡片 / 表单 / 看板 / 文档页，或工具栏里带 `<i>` 图标与多行结构 → **本模块**
 *
 * ## 为什么要有这一层
 *
 * 对标物的每个「页面」其实是**服务端吐出的 laytpl 模板**：父框架把 HTML 取回来，
 * 用 laytpl 渲染后再插入。所以扒下来的原始 HTML 里有三类东西必须处理：
 *  1. `{{! ... !}}` —— laytpl **注释**（对标物的死代码常整块躺在这里，例如那 8 个 `switchTpl`），
 *     浏览器不会执行它，但**照抄会变成可见文字**；
 *  2. `{{= i18n.$t('x') }}`、`{{# if (...) { }}` —— laytpl 表达式/逻辑，
 *     中文要取**该页 JS 里的 `i18nData.messages['zh-CN']`** 字典（值纪律：中文不许自己写）；
 *  3. `<script>`（页面逻辑，不要搬）与 `<script type="text/html" id="...">`（**工具栏/列模板，是标记，要搬**）。
 *
 * 所以每个页面模块暴露的是**可核对的静态证据件**：`styles`（页内 `<style>` 原文）
 * + `body`（已解表达式的标记）+ `tables`（从页面 JS 的 `cols` 抄来的表头）。
 * `.tools/check-replicas.mjs` 拿这三样与对标产物逐项对拍——这是「值一律从对标取」的可复跑证据。
 *
 * ## 行为口径（D-0025 §5）
 *
 * 表格**恒空**、不发任何写请求；页内任何按钮/开关被点到，统一提示「「X」尚未开放」。
 */
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  islandTabTarget,
  type IslandContext,
  type IslandPage,
} from './island'

export type ArtifactCol = {
  /**
   * 对标 `cols[i].type`：特殊列（勾选框 / 单选 / 序号 / 空占位）。
   *
   * ⚠️ **必须显式声明**：少了它这些列会**静默消失**（表头勾选框、序号列都没有了），
   * 而 `parity.mjs` 的表头比对只读**文案 + 宽度**——空文案的特殊列即使整个丢掉也不会报差异。
   * 判据：对标页面 JS 的 `cols` 里带 `type:'checkbox'|'radio'|'numbers'|'space'` 就要抄。
   */
  type?: 'checkbox' | 'radio' | 'numbers' | 'space'
  /** 对标 `cols[i].field`（纯占位列可不填）。 */
  field?: string
  /** 表头文案：必须是**页内 i18n 字典解出来的中文**，不是 `i18n.$t(...)` 原文。 */
  title?: string
  width?: number | string
  minWidth?: number | string
  /** 对标里 `hide:true` 的列：默认不显示。 */
  hide?: boolean
  align?: 'left' | 'center' | 'right'
  fixed?: 'left' | 'right'
  sort?: boolean
  /** 对标写 `nresize`（拼错）无效；写 `unresize: true` 才生效。别照抄拼写错误。 */
  unresize?: boolean
  /**
   * 对标**列级** `toolbar`（行内操作按钮的模板选择器，如 `'#baroperating'`）。
   * 少了它，操作列会静默变成空列——与漏 `type` 同一类陷阱（空表下不可见，探针也不报）。
   */
  toolbar?: string
  /** 对标 `cols[i].edit`（单元格直接编辑）。 */
  edit?: boolean
  /**
   * 对标 `cols[i].templet`：**字符串形式**（指向页内 `<script type="text/html" id="…">` 模板）。
   * 函数形式的 templet 不要抄（我们的表恒空，且函数会把对标的运行期逻辑带进来）——
   * 开关列用 `switchText`、徽标列用 `badge` 表达。给了 `templet` 时优先用它。
   */
  templet?: string
  /** 对标 `cols[i].totalRow` / `totalRowText`（合计行）。 */
  totalRow?: boolean
  totalRowText?: string
  /** 开关列：`lay-text` 原文，如 `'正常|禁用'`（空表下不渲染，仅备查）。 */
  switchText?: string
  /** 徽标列：用页内 `.lableb` 渲染（空表下不渲染，仅备查）。 */
  badge?: string
}

export type ArtifactTable = {
  /** 页内隐藏表的 id 选择器，如 `'#tableList_user'`（与对标 `table.render` 的 `elem` 同值）。 */
  elem: string
  cols: ArtifactCol[]
  /** 工具栏模板选择器，如 `'#toolbarDemo'`（模板本身在 `body` 里）。 */
  toolbar?: string
  /** 对标 `page.limit`。 */
  limit?: number
  pageLimits?: number[]
  /**
   * 对标 `page` 配置的**原样**：`false` = 不分页（对标有 7 页这样写）。
   * 不传则用 `{ limit: limit ?? 20, limits: pageLimits ?? [10,20,30,40,50] }`。
   *
   * ⚠️ **`page.limits` 一律取自该页 JS**（如人员管理页是 `[10,20,30,40,50,100]`，取自
   * `userlist.js:815`）。用默认值会让「条/页」下拉少一项、宽度也差几像素——探针会报
   * `option 数 / 宽度` 差异。`limit` 在对标里是运行期变量时取一个合理定值并注释说明。
   */
  page?: boolean | { limit?: number; limits?: number[] }
  /**
   * 对标 `defaultToolbar`。⚠️ 对标**在同一个 `render` 对象里写了两次**（后写的生效），
   * 生效值多为 `['filter']`（表格右上角出现「筛选列」图标）；只有一页是真 `[]`。
   * 不传 = `[]`（没有那个图标）。
   */
  defaultToolbar?: unknown[]
  /**
   * 对标**表级** `totalRow`（合计行）。⚠️ 与列级 `ArtifactCol.totalRow` 是两回事：
   * 表级那条会在表底渲染一行 `.layui-table-total`（实测高 38px）。0 行数据时它带
   * `layui-hide-v` 不可见，但**仍占 38px**——漏了会让整页高度、以及其后所有元素的 y 都差 38px。
   * 值是 `true` 或对标给的合计配置对象。
   */
  totalRow?: boolean | Record<string, unknown>
  /** 对标 `cellMinWidth`（只影响自动列的最小宽度）。 */
  cellMinWidth?: number
  /**
   * `table.render` 的 `done` 回调（对标常在这里往 `#total` 之类的统计槽写文案骨架）。
   * 渲染完会先跑 `form.render()`，再调它——**顺序与对标一致**（对标写在 done 里）。
   */
  done?: (res: unknown) => void
  /** 对标 `height`（`'full-100'` 之类要按对标原样给）。 */
  height?: number | string
}

export type ArtifactSpec = {
  /** 对标页 `<style>` 段落**原文**（含 `.layui-table-cell` / `.lableb` 这类页内覆盖）。 */
  styles?: string
  /** 对标页 body 标记（已去脚本、已解 laytpl 表达式与注释）。 */
  body?: string
  /**
   * **推荐入口**：把整份证据件放在 `public/replica/<key>.html`（`<style>` 段 + body 标记），
   * 运行时取回再注入。
   *
   * 为什么用文件而不是内联 `body` 字符串：对标页的 HTML/CSS 里会出现反引号与 `${`
   * （实测 `system-systemsetting` 的 body 有 94KB），内联进模板字符串必须转义、且 review 时
   * 无法与对标产物逐行对拍。放成静态 HTML 则**零转义**，且 `.tools` 的核对脚本能直接读它。
   */
  url?: string
  tables?: ArtifactTable[]
}

/**
 * layui **自己的**默认分页配置，两侧构建同值（实测：`public/layui/layui.js` 偏移 279687，
 * 对标 `.tools/recon/suqing1/assets/layuiadmin/layui/layui.js` 偏移 279215，同串）：
 *   `config.limit = 10`、`limits: i.limits || [10,20,30,40,50,60,70,80,90]`
 *
 * ⚠️ **但这两个值只在分页开启时才有意义**：layui 的默认 `config` 里**没有 `page` 键**，
 * 未写 `page` 即 falsy → **分页关闭**（分页闸门见对标构建 offset 296935 的 `void(i.page&&(…))`）。
 * 全批实测：`page:false` 或未写 `page` 的共 **44 张表 / 38 页**（显式 false 40 + 未写 4），
 * 分页开启 49 张表。所以**别把这里的兜底当成"对标默认会分页"**——不写 page 的表根本不出分页栏。
 * 另：「0 行的表无论 page 真假都不渲染 `.layui-laypage`」（offset 296680 的 `0==n` 分支），
 * 所以**用渲染结果判 page 真假必须挑有数据的表**，空表会得假阴性。
 */
const DEFAULT_LIMIT = 10
const DEFAULT_LIMITS = [10, 20, 30, 40, 50, 60, 70, 80, 90]
/**
 * layui 的 `defaultToolbar` 默认值，**顺序逐字**取自同一处 `config`（offset 279215）：
 * `["filter","exports","print"]` —— 即表格右上角的「筛选列 / 导出 / 打印」三个图标。
 * 对标没写 `defaultToolbar` 的表吃到的就是它；对标写了的地方由草稿导出真值覆盖。
 */
const DEFAULT_TOOLBAR = ['filter', 'exports', 'print']

function buildCol(c: ArtifactCol): Record<string, unknown> {
  const col: Record<string, unknown> = {}
  // `type` 要最先写：layui 对特殊列只看 `type`，给了它就不再需要 field/templet。
  if (c.type !== undefined) col.type = c.type
  if (c.field !== undefined) col.field = c.field
  if (c.title !== undefined) col.title = c.title
  if (c.width !== undefined) col.width = c.width
  if (c.minWidth !== undefined) col.minWidth = c.minWidth
  if (c.hide === true) col.hide = true
  if (c.align !== undefined) col.align = c.align
  if (c.fixed !== undefined) col.fixed = c.fixed
  if (c.sort === true) col.sort = true
  if (c.unresize === true) col.unresize = true
  if (c.toolbar !== undefined) col.toolbar = c.toolbar
  if (c.edit === true) col.edit = true
  if (c.totalRow !== undefined) col.totalRow = c.totalRow
  if (c.totalRowText !== undefined) col.totalRowText = c.totalRowText
  // 空表下单元格不渲染，但留着 templet 才能在将来接上真数据时不再返工。
  if (c.templet) {
    col.templet = c.templet
  } else if (c.switchText) {
    col.templet = () =>
      `<input type="checkbox" lay-skin="switch" lay-text="${c.switchText}" disabled>`
  } else if (c.badge) {
    col.templet = () => `<span class="lableb">${c.badge}</span>`
  } else {
    col.templet = () => ''
  }
  return col
}

/** 页内可点元素的「名字」：优先 `lay-tips`（对标自带的说明），否则取可见文字。
 *
 * ⭐ 导出（系统开关线，D-0223）：`system-systemsetting.ts` 自建「尚未开放」
 * 总闸时复用**同一份**取名字逻辑（同一句 toast 的文案只有一处实现，`AGENTS.md` §5-E）。 */
export function clickLabel(el: HTMLElement): string {
  const tips = el.getAttribute('lay-tips')
  if (tips) return tips.length > 12 ? `${tips.slice(0, 12)}…` : tips
  return (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 16)
}

/**
 * 取回一份**证据件**（`public/replica/<key>.html`）的原文。
 *
 * 用 `BASE_URL` 拼，构建在子路径（如 `/console/`）时才不会取错。
 * 从 `mountArtifact` 里抽出来是为了让**需要自己 mount 的页**（如
 * `layui-island/pages/system-roleauthority.ts`）复用同一份路径处理，不复制粘贴。
 */
export async function fetchArtifactHtml(url: string): Promise<string> {
  const res = await fetch(`${import.meta.env.BASE_URL}${url.replace(/^\//, '')}`)
  if (!res.ok) throw new Error(`证据件取不到（HTTP ${res.status}）：${url}`)
  return res.text()
}

/** 挂载「证据件复刻页」。返回卸载函数。 */
export async function mountArtifact(
  root: HTMLElement,
  spec: ArtifactSpec,
): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as Record<string, unknown>
  const table = layui.table as Record<string, unknown>
  const layer = layui.layer as Record<string, unknown>

  // `url` 优先：证据件是静态 HTML（`public/replica/<key>.html`），取回后整段注入。
  let artifactHtml = ''
  if (spec.url) {
    artifactHtml = await fetchArtifactHtml(spec.url)
  } else {
    artifactHtml = `${spec.styles ? `<style>${spec.styles}</style>` : ''}${spec.body ?? ''}`
  }

  root.innerHTML = artifactHtml
  // 页签/折叠面板要等标记进来之后再绑定（详见 `initIslandElements` 的注释）。
  initIslandElements(layui)

  for (const t of spec.tables ?? []) {
    ;(table.render as (o: unknown) => void)({
      elem: t.elem,
      data: [],
      cols: [t.cols.map(buildCol)],
      ...(t.toolbar ? { toolbar: t.toolbar } : {}),
      // 兜底 = layui 自己的默认 `["filter","exports","print"]`（实测对标构建
      // `.tools/recon/suqing1/assets/layuiadmin/layui/layui.js` offset 279215 的
      // `defaultToolbar:["filter","exports","print"]`；我们 vendored 的同串）。用 `[]` 会让
      // 「对标没写 defaultToolbar」的表少三个图标。对标显式声明的表由草稿导出真值覆盖。
      defaultToolbar: t.defaultToolbar ?? DEFAULT_TOOLBAR,
      ...(t.totalRow !== undefined ? { totalRow: t.totalRow } : {}),
      page:
        t.page !== undefined
          ? t.page
          : { limit: t.limit ?? DEFAULT_LIMIT, limits: t.pageLimits ?? DEFAULT_LIMITS },
      ...(t.cellMinWidth !== undefined ? { cellMinWidth: t.cellMinWidth } : {}),
      ...(t.height !== undefined ? { height: t.height } : {}),
      text: { none: '暂无数据' },
      done: (res: unknown) => {
        ;(form.render as () => void)()
        t.done?.(res)
      },
    })
  }
  ;(form.render as () => void)()

  // 点了就提示「尚未开放」：按钮、开关、行内链接算；**会被 island 接管的页签**不算。
  //
  // ⚠️ **页签（`.layui-tab-title li`）分两类，不能一刀切**（2026-09-22 第五轮实测）：
  //  ① **有面板的页签**（`/System/systemsetting`、`/User/myinfo`）
  //     ——面板切换是纯客户端结构行为、不是功能，拦了并 `stopPropagation`
  //     会让第二个面板永远看不到 ⇒ **不弹**。
  //     （2026-09-23 短信整块下线删掉了原第 4 例 `/Sms/smsbulkhair`；同日下线的 4 个系统设置页
  //     又删掉了 `/System/wechartsetting` ⇒ 此处剩两例，与
  //     `layui/__tests__/artifact-tab-title.spec.ts` 的 `PANEL_PAGES` 两条一一对应。）
  //  ② **没有面板的页签**（证据件只有 `<ul class="layui-tab-title">`，**没有**
  //     `.layui-tab-content`/`.layui-tab-item`）——`island.ts` 的页签处理器
  //     在这种页签上**不接管**（它只切 `layui-this`/`layui-show`，没有面板就什么都不发生）
  //     ⇒ 本 catch-all 是它**唯一**的反馈，**必须照旧弹**「尚未开放」。
  //     （今天两类都为空集：原第 ② 例 `/Customer/hylevelsetting` 已随 2026-11-10 第二批
  //     整页删除；集合由 `layui/__tests__/artifact-tab-title.spec.ts` 按源码推导并钉空。）
  //
  // 判据 = `islandTabTarget(target)`（**唯一出处**在 `island.ts`，与 `initIslandElements`
  // 调的是同一个函数）。⛔ **不许退回上一版的全局排除** `target.closest('.layui-tab-title')`：
  // 那会把第 ② 类页面的提示一起吞掉，与 D-0025 的「无后端部分点了提示尚未开放」直接矛盾。
  //
  // ⚠️ **必须用捕获阶段**（第三参 `true`）：对标的工具栏标记里带 `lay-event`，
  // 而 layui 的表格模块对 `.layui-table-tool *[lay-event]` 自己做了委托并 `return false`
  // （实测：等于 preventDefault + stopPropagation）——挂在冒泡阶段的处理器**收不到事件**，
  // 症状是「按钮点了没反应」。捕获阶段先于它的委托，拿到后直接 `stopPropagation` 收口。
  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement
    const el = target.closest<HTMLElement>(
      'button, a.layui-btn, .layui-form-switch, [lay-event], [lay-filter]',
    )
    if (!el || el.hasAttribute('disabled')) return
    // 页签容器的 `lay-filter`（`div.layui-tab[lay-filter=…]`）或外层 `<form lay-filter>`
    // 会让上面那个 `closest` **向上命中容器** ⇒ 点页签被判成「尚未开放」控件。
    // 判据必须打在**被点的元素**上（`target`）：容器在 `.layui-tab-title` 的**外面**，
    // `el.closest('.layui-tab-title')` 恒为 null（实测）。
    // ⛔ 也不要把 `island.ts` 的「不吞事件」改回去兜这个——页面自己的页签取数依赖事件能到 root。
    if (islandTabTarget(target) !== null) return
    event.preventDefault()
    event.stopPropagation()
    islandMsg(layer, `「${clickLabel(el) || '该功能'}」尚未开放`)
  }
  root.addEventListener('click', onClick, true)

  return () => {
    root.removeEventListener('click', onClick, true)
    root.innerHTML = ''
  }
}

/** 把一份 spec 包成标准 island 页模块（默认导出给 `layui-island/page.ts`）。 */
export function artifactPage(spec: ArtifactSpec): IslandPage {
  return {
    mount: (root: HTMLElement, _ctx: IslandContext) => mountArtifact(root, spec),
  }
}
