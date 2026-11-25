/**
 * **声明式「结构复刻页」**：对标能打开、但我们没有后端接口的那些页面，用这一份渲染器统一渲染。
 *
 * 覆盖的形状：**筛选栏 + 工具栏（+ 统计文案）+ 表格（+ 勾选列 + 操作列）**——
 * 这是鸡蛋铺大多数列表页的骨架（见 `docs/FRONTEND-REPLICA-STANDARD.md` §4）。
 * 有额外结构（页签 / 卡片 / 复杂弹窗）的页面，写自己的页面模块，别硬塞进这里。
 *
 * 行为口径（D-0025 §5）：**照对标渲染，点了提示「尚未开放」**；表格恒空、不发任何写请求。
 */
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from './island'

export type ReplicaFilter =
  | {
      kind: 'select'
      label: string
      name: string
      width: number
      options: readonly { label: string; value: string | number }[]
    }
  | { kind: 'text'; label: string; name: string; width: number; placeholder?: string }
  | { kind: 'date'; label: string; name: string; width: number; placeholder?: string }

/** 对标按钮配色：不传=默认绿 / `primary`=蓝(`layui-btn-normal`) / `danger`=红 / `warning`=橙(`layui-btn-warm`)。 */
export type ReplicaButtonType = 'primary' | 'success' | 'warning' | 'danger'

export type ReplicaButton = { label: string; type?: ReplicaButtonType }

export type ReplicaCol = {
  /** 对标 `cols` 里的 `field`（纯占位列可以不填）。 */
  field?: string
  title: string
  /**
   * 定宽。**只有对标那条列真的写了 `width` 时才填。**
   *
   * ⚠️ 对标页面里有 5 列写的是 **`minwidth`（小写 w）**，而 layui 只认 `minWidth`——
   * 那 5 个 `minwidth` **全部无效**，它们在 layui 眼里是「自动列」，由 layui 把
   * 「表宽 − 定宽列之和」**均分**。给这种列补一个 `width`/`minWidth` 会让自动列被撑开、
   * 总宽超过视口 → 出现横向滚动条（`/User/userlist` 实测：对标总宽 1649、被"修好"的那版 1669）。
   * **正确的复刻方式是三样都不填**，让它保持自动列。
   */
  width?: number
  /** 同上：对标写的是无效的 `minwidth` 时**不要**在这里补 `minWidth`。 */
  minWidth?: number
  /** 对标里 `hide:true` 的列：**默认不显示**。 */
  hide?: boolean
  align?: 'left' | 'center' | 'right'
  /** 渲染成 layui 开关（对标那 8 个开关列都是这个形状）。 */
  switchText?: string
}

export type ReplicaSpec = {
  /**
   * 页标题（对标服务端渲染的 `<title>`，如 `/User/userlist` 是「系统用户管理」）。
   * island 的外壳页只有一个静态 `<title>`，所以按页覆盖；不传就不动。
   */
  title?: string
  /** 筛选栏控件（按对标顺序）。 */
  filters: ReplicaFilter[]
  /** 搜索按钮；不传就按对标的默认：蓝、宽 80、文案「搜索」。 */
  searchButton?: { label: string; width?: number } | false
  /** 工具栏按钮（按对标原文顺序）。 */
  toolbar: ReplicaButton[]
  /** 工具栏下方那行文案（对标常放统计，如「本系统内共有 N 个…」；色 `#ff6a00`）。 */
  toolbarNote?: string
  /** 表格列（按对标 `cols` 顺序）。 */
  cols: ReplicaCol[]
  /** 是否要左侧勾选列（对标多数列表页有）。 */
  checkbox?: boolean
  /** 操作列（可选）：按钮文案列表，渲染成 `layui-btn-xs`，点了提示未开放。 */
  actions?: string[]
  /**
   * 对标 `page.limits`。⚠️ **一律取自该页 JS**（人员管理页是 `[10,20,30,40,50,100]`，
   * 取自 `userlist.js:815`）；用默认值会让「条/页」下拉少一项、宽度差几像素。
   */
  pageLimits?: number[]
  /** 对标 `page` 的原样：`false` = 不分页。不传则用 `{limit: 20, limits: pageLimits}`。 */
  page?: boolean | { limit?: number; limits?: number[] }
  /** 对标 `defaultToolbar`（同一 render 对象里写两次时**后写的**生效，多为 `['filter']`）。不传 = `[]`。 */
  defaultToolbar?: unknown[]
  /**
   * 整页 `<form class="layui-form">` 内部、表格**之后**那些 `display:none` 的容器标记
   * （对标每个列表页都有自己的弹层 / 卡片，如 `.recPricebox` / `.typebox` / `.layui-card.xxxDiv`）。
   * **逐字**从对标页面 `<form>` 里取。
   *
   * 为什么值得带上：对标把它们放在同一个 `<form>` 里（`/User/userlist` 实测 `<form>` L111 开、
   * L421 闭），而且探针是按**文档级选择器**统计元素的——少一段隐藏标记，
   * `.layui-form-label` / `.layui-input-inline` 的条数就直接少几条。
   * 里面的子表（`.layui-hide` 的 `<table>`）只放标记、**不要**调 `table.render`。
   */
  hidden?: string
}

const TYPE_CLASS: Record<ReplicaButtonType, string> = {
  success: '',
  primary: ' layui-btn-normal',
  danger: ' layui-btn-danger',
  warning: ' layui-btn-warm',
}

function filterHtml(f: ReplicaFilter): string {
  let control: string
  if (f.kind === 'select') {
    const opts = f.options
      .map((o) => `<option value="${o.value}">${o.label}</option>`)
      .join('')
    control = `<select name="${f.name}">${opts}</select>`
  } else {
    const ph = f.placeholder ? ` placeholder="${f.placeholder}"` : ''
    control = `<input type="text" name="${f.name}" class="layui-input"${ph}>`
  }
  return (
    `<div class="layui-inline">` +
    `<label class="layui-form-label" style="width:80px">${f.label}</label>` +
    `<div class="layui-input-inline" style="width:${f.width}px">${control}</div>` +
    `</div>`
  )
}

/**
 * 筛选栏**内部**标记（不含 `<form>` 外壳——外壳由 `mountReplicaPage` 包住整页）。
 *
 * ⚠️ 搜索按钮外面那层 `.layui-input-inline` 是**对标原文**（`/User/userlist` L230-237：
 * `<div class="layui-inline"><div class="layui-input-inline" style="">…<button …>`）。
 * 少了它，探针里 `.layui-input-inline` 就少一个（对标 17 / 我们 16），
 * 而且按钮左右的间距也会差一点。
 */
function filtersHtml(spec: ReplicaSpec): string {
  let searchHtml = ''
  if (spec.searchButton !== false) {
    const search = spec.searchButton ?? { label: '搜索' }
    searchHtml =
      `<div class="layui-inline">` +
      `<div class="layui-input-inline" style="">` +
      `<button class="layui-btn layui-btn-normal" data-notopen="${search.label}" type="button" style="width:${search.width ?? 80}px">${search.label}</button>` +
      `</div>` +
      `</div>`
  }
  return `
    <div style="padding-left:5px;padding-top:10px">
      <div class="layui-form-pane">
        ${spec.filters.map(filterHtml).join('')}
        ${searchHtml}
      </div>
    </div>`
}

function toolbarHtml(spec: ReplicaSpec): string {
  const buttons = spec.toolbar
    .map(
      (b) =>
        `<button class="layui-btn layui-btn-xs${TYPE_CLASS[b.type ?? 'success']}" style="margin-right:5px" data-notopen="${b.label}" type="button">${b.label}</button>`,
    )
    .join('')
  const note = spec.toolbarNote
    ? `<div style="color:#ff6a00" id="total">${spec.toolbarNote}</div>`
    : ''
  return `<div class="layui-btn-container" id="pageToolbar">${buttons}</div>${note}`
}

function buildCols(spec: ReplicaSpec): Record<string, unknown>[] {
  const cols: Record<string, unknown>[] = []
  if (spec.checkbox !== false) cols.push({ type: 'checkbox', fixed: 'left', width: 30 })
  for (const c of spec.cols) {
    const col: Record<string, unknown> = { title: c.title }
    if (c.field) col.field = c.field
    if (c.width !== undefined) col.width = c.width
    if (c.minWidth !== undefined) col.minWidth = c.minWidth
    if (c.hide === true) col.hide = true
    if (c.align !== undefined) col.align = c.align
    if (c.switchText) {
      col.templet = () =>
        `<input type="checkbox" lay-skin="switch" lay-text="${c.switchText}" disabled>`
    } else {
      col.templet = () => ''
    }
    cols.push(col)
  }
  if (spec.actions?.length) {
    cols.push({
      title: '操作',
      width: 60 + spec.actions.length * 44,
      templet: () =>
        spec
          .actions!.map(
            (a, i) =>
              `<a class="layui-btn layui-btn-xs${i === 0 ? '' : ' layui-btn-warm'}" style="margin-left:${i === 0 ? 0 : 2}px" data-notopen="${a}">${a}</a>`,
          )
          .join(''),
    })
  }
  return cols
}

/** 挂载「结构复刻页」。返回卸载函数。 */
export async function mountReplicaPage(
  root: HTMLElement,
  spec: ReplicaSpec,
): Promise<() => void> {
  const layui = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as Record<string, unknown>
  const table = layui.table as Record<string, unknown>
  const layer = layui.layer as Record<string, unknown>

  if (spec.title) document.title = spec.title

  root.innerHTML = `
    <form class="layui-form" action="" lay-filter="sform" onsubmit="return false">
      ${filtersHtml(spec)}
      <div style="">
        <table class="layui-hide" id="pageTable" lay-filter="pageTable"></table>
      </div>
      ${spec.hidden ?? ''}
    </form>
    <script type="text/html" id="pageToolbarTpl">${toolbarHtml(spec)}</script>`

  const onClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('[data-notopen]')
    if (!el) return
    islandMsg(layer, `「${el.dataset.notopen ?? ''}」尚未开放`)
  }
  root.addEventListener('click', onClick)

  initIslandElements(layui)
  ;(form.render as () => void)()
  ;(table.render as (o: unknown) => void)({
    elem: '#pageTable',
    data: [],
    toolbar: '#pageToolbarTpl',
    // 兜底 = layui 的默认 `["filter","exports","print"]`（实测对标构建 offset 279215，
    // 见 `artifactPage.ts` 的 `DEFAULT_TOOLBAR` 注释）。对标写 `['filter']` 是常态，要显式传。
    defaultToolbar: spec.defaultToolbar ?? ['filter', 'exports', 'print'],
    page:
      spec.page !== undefined
        ? spec.page
        : // 兜底 = **layui 自己的默认**（`limit:10` / 9 项 limits，见 `artifactPage.ts` 的
          // `DEFAULT_LIMITS` 注释与实测偏移）。用「5 项 + 20」会让没声明 limits 的页少 4 个选项。
          { limit: 10, limits: spec.pageLimits ?? [10, 20, 30, 40, 50, 60, 70, 80, 90] },
    cols: [buildCols(spec)],
    text: { none: '暂无数据' },
    done: () => (form.render as () => void)(),
  })

  return () => {
    root.removeEventListener('click', onClick)
    root.innerHTML = ''
  }
}

/** 把一份 spec 包成标准 island 页模块（注册到 `layui-island/page.ts`）。 */
export function replicaPage(spec: ReplicaSpec): IslandPage {
  return {
    mount: (root: HTMLElement, _ctx: IslandContext) => mountReplicaPage(root, spec),
  }
}
