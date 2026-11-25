/**
 * 【录单自动识别设置】页的 **layui 复刻页**（对标物 `/System/autodiscernset`，**证据件标记 + 真 API**）。
 *
 * 形状：**单表列表页**（**没有工具栏** + 表格 + 一个 `display:none` 的关键词编辑弹层）。
 * 本模块把表格接到后端 `/api/business/recognition-settings/`（spec §5 / §7），
 * 弹层照对标参数打开，保存走 `PATCH …/<item_code>/`。
 *
 * 证据（值一律从对标取）：
 *  - 标记与页内 `<style>` → `public/replica/system-autodiscernset.html`
 *    （表单与弹层：L86-111；表格与 `#baroperating`：L88-91）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/System/autodiscernset.js` **L19-47**
 *  - 弹层打开参数（`changeData`）→ 同文件 **L88-103**
 *
 * ## 为什么不走 `artifactPage`（spec §3-D6）
 *
 * `artifactPage` 装了一个**捕获阶段**的「尚未开放」总闸（`src/layui/artifactPage.ts:259-269`），
 * 它把页内一切按钮/开关的点击先截走（`preventDefault + stopPropagation`）⇒ 真数据下行内那颗
 * 「修改关键词」点了没反应。本页改用「自取证据件 + 自有委托」（先例：`system-sourelist.ts`），
 * 并显式给 `table.render` 传对标取值（`defaultToolbar: []`、`page: false`、**不传 `toolbar`**）。
 *
 * ## 三条最容易写错、且写错后**静默**失效的机制（全部实测/读码确认）
 *
 * | # | 机制 | 写错的后果 |
 * |---|---|---|
 * | 1 | 弹层表单的 `lay-filter` 是 **`dis`**（证据件 L86），预填用 `form.val('dis', …)` | 写成 `'form'` ⇒ 预填**静默**不生效（打开是空框） |
 * | 2 | 保存**必须从弹层 DOM 读值**（`[name="key"]`），不能读 layui 的 `data.field` | 读值恒为 `''`（见下） |
 * | 3 | `table.on('tool(tableList)')` 的 filter 是 **`tableList`**；`table.reloadData` 首参是 **id**（不带 `#`） | 传 `#tableList` ⇒ 静默 no-op |
 *
 * 第 2 条的读码依据：`public/layui/layui.js` 的 `t.prototype.submit` 用
 * `a.parents(form).eq(0)` 取表单再 `r.getValue(null, n)`；而 `layer.open({type:1, content: jq})`
 * 会把 `.change` 节点**搬出 `<form>`** ⇒ `parents(form)` 为空集 ⇒ `data.field` 是 `{}`。
 * 仓库先例：`system-sourelist.ts::submit` 同样从面板 DOM 读，不读 `data.field`。
 *
 * ## 其他必须知道的口径
 *
 * - **表格数据由页面自己取**（`fetchRecognitionSettings()` → `table.reloadData`），
 *   **不给** layui 传 `url`：统一信封与 CSRF/错误映射都在 `api/` 里，绕开它等于再造一套；
 *   且读码可知只要 config 里还有 `url`，`reloadData(id, {data})` 会走 ajax 分支、忽略传进来的 `data`
 *   （`system-sourelist.ts` 文件头已记同一条）。
 * - **行内按钮模板 `#baroperating` 就在证据件里**（L89-91），操作列用 `toolbar: '#baroperating'`
 *   引用它即可，**不**在 TS 里另拼一份 HTML。
 * - **两条开弹层的路**（对标就是两条）：行内「修改关键词」（`table.on('tool')` 的 `submitEdit`）
 *   与第一列蓝字（`.editrow` 的委托点击）。
 * - **当前编辑的是哪一项**用页面闭包变量 `editingItemCode` 记住，**不依赖 `#disID`**：
 *   它是弹层 DOM 的一部分，节点被搬走后读它等于把「当前行」这件事实寄托在渲染状态上。
 *
 * ## 与对标并排比对的差异（原因与证据等级）
 *
 * | # | 差异 | 原因 |
 * |---|---|---|
 * | 1 | `data-disid` 里装**我们的 `item_code`**（对标装它的数字 `disId`） | 属性名照抄以保持标记一致，值换我们的稳定标识（spec §7 第 5 条） |
 * | 2 | 列表**不重排** | 顺序由后端固定返回（14 行、顺序即页面顺序，spec §1.2）；前端再排一遍 = 同一事实两套判据 |
 * | 3 | 关键词列的 `<a>` **不挂事件** | 对标也只有 `typeName` 那一列挂了 `.editrow`（`data-*` 只在那条 templet 上） |
 */
import {
  fetchRecognitionSettings,
  updateRecognitionKeywords,
  type RecognitionItemRow,
} from '@/api/recognition'
import { findPanel, loadConfigArtifact, openPanelLayer } from '@/layui-island/config-replica'
import { escapeHtml } from '@/layui-island/escape'
import {
  bootstrapIsland,
  initIslandElements,
  islandMsg,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** 页 `<title>`（对标服务端渲染值，证据件源 `System__autodiscernset.html` L5，逐字）。 */
export const TITLE = '录单自动识别设置'
/** 证据件名（= island key = `pages/<key>.ts` 的文件名）。 */
const ARTIFACT_KEY = 'system-autodiscernset'
/**
 * 弹层容器的**类名**（对标 `$('.change')`，证据件 L94）。
 * ⛔ 与 `PANEL_SELECTOR` **分成两个常量**：`class="${PANEL_SELECTOR}"` 会造出
 *    `class=".change"`，选择器永远匹配不到、弹层**静默**打不开（E-0061 ①）。
 */
export const PANEL_CLASS = 'change'
/** 同一个容器的 CSS 选择器（带 `.`）。 */
export const PANEL_SELECTOR = '.change'
/** 表格 `elem`（`autodiscernset.js` L20）。 */
export const TABLE_ELEM = '#tableList'
/** `table.reloadData` 的第一参要的是 **id**（不带 `#`），传 `#id` 是静默 no-op（E-0061 ③）。 */
export const TABLE_ID = 'tableList'
/** 弹层表单的 `lay-filter`（证据件 L86 是 `dis`——**不是** `form`，写错会让预填静默失效）。 */
export const FORM_FILTER = 'dis'
/** 第一列蓝字链接的类名（对标 templet 里的 `class="editrow"`）。 */
export const EDITROW_SELECTOR = '.editrow'
/** 行内按钮模板的 id（证据件 L89-91）；操作列用 `toolbar: '#baroperating'` 引用它。 */
export const BAROPERATING_ID = 'baroperating'

/** 页面自己用的行形状（键名是我们自己的；`cols[].field` 以它取值）。 */
export type RecognitionRow = { itemCode: string; typeName: string; disKey: string }

/** 接口行 → 页面行（字段名映射在**页面**做：接口一律 snake_case，列 `field` 是对标拼写）。 */
export function toRow(item: RecognitionItemRow): RecognitionRow {
  return { itemCode: item.item_code, typeName: item.name, disKey: item.keywords }
}

/**
 * 列骨架（顺序/标题/宽度/对齐/`fixed`/`toolbar`）——逐条照 `autodiscernset.js` L21-47。
 *
 * ⚠️ `fixed: ''`（不固定）与 `sort: false` **照写**（后者本就是 layui 默认值，但对标原文写着，
 * 别「顺手」删掉——删了就看不出这是照抄来的）。
 */
export const COLS: Record<string, unknown>[] = [
  { field: 'typeName', title: '识别项名称', fixed: 'left', width: 100, align: 'left', unresize: true },
  {
    field: 'disKey',
    title: '订单内容中每一行可以识别的关键词',
    align: 'left',
    sort: false,
    unresize: true,
  },
  { title: '操作', fixed: '', width: 120, toolbar: `#${BAROPERATING_ID}`, unresize: true },
]

/**
 * 三列的**真实渲染**声明：骨架来自 `COLS`（唯一出处），`templet` 只按 `field` 挂数据渲染。
 *
 * ⚠️ `templet` 的返回值被 layui **当 HTML 插入**，而名称/关键词都是可编辑文本 ⇒ **必须转义**
 * （`@/layui-island/escape` 的唯一出处；不转义就是存储型 XSS，同 `system-sourelist.ts` 的 I-4）。
 */
export function buildCols(): Record<string, unknown>[] {
  const cells: Record<string, (row: RecognitionRow) => string> = {
    // 第一列：蓝色链接，`data-disid` 里装的是**我们的 item_code**（对标装的是它的数字 disId；
    // 属性名照抄以保持标记一致，值是我们的稳定标识）。
    typeName: (res) => {
      const name = escapeHtml(String(res.typeName ?? ''))
      const code = escapeHtml(String(res.itemCode ?? ''))
      const keys = escapeHtml(String(res.disKey ?? ''))
      return (
        `<a href="javascript:void(0);" class="editrow" data-disid="${code}" ` +
        `data-typename="${name}" data-diskey="${keys}" style="color:#1E9FFF" title="${name}">${name}</a>`
      )
    },
    // 第二列：同形 `<a>`（含对标的**空 `class=""`**，不是我们漏了）。
    disKey: (res) => {
      const keys = escapeHtml(String(res.disKey ?? ''))
      return `<a href="javascript:void(0);" class="" title="${keys}">${keys}</a>`
    },
  }
  return COLS.map((c) => {
    const field = typeof c.field === 'string' ? c.field : ''
    const cell = cells[field]
    return cell ? { ...c, templet: cell } : { ...c }
  })
}

/** 弹层参数：逐条照 `autodiscernset.js` L88-103 的 `changeData`（`type:1` 与 `content` 由 `openPanelLayer` 补）。 */
export function dialogOptions(row: RecognitionRow, content: unknown): Record<string, unknown> {
  return {
    title: `修改${row.typeName}的识别关键词`,
    shade: [0.3, '#333'],
    shadeClose: true,
    skin: 'layui-layer-lan',
    anim: 8,
    area: ['80%', '180px'],
    content,
  }
}

/* ────────────────────────────── 挂载 ────────────────────────────── */

export async function mountRecognitionPage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ **先挂样式（`bootstrapIsland`）再塞标记（`root.innerHTML`）**：`layui.css` 是
  // `bootstrapIsland()` 运行期插 `<link>` 的，顺序反了会先画出没穿样式的骨架再闪成正常
  // （FOUC，E-0067）。`initIslandElements` 相反，**必须**留在塞标记之后（它绑的是标记里的页签）。
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule

  document.title = TITLE

  const msg = (text: string): void => islandMsg(layer, text)
  const errText = (e: unknown): string => (e instanceof Error ? e.message : '操作失败')

  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  initIslandElements(layui)

  /* ── 弹层节点 ──
   *
   * ⚠️ 证据件里 `.change` 是**真实存在的**（L94，`display:none`），但表格重绘可能连带把它
   * 重建/摘掉。所以留一个**模板快照**：节点被摘掉时从快照补一个**干净副本**回 `<form>`——
   * 没有这一步，重载后弹层会静默打不开（`openPanelLayer` 找不到节点、回 `null`）。
   * 有快照时**不覆盖**现存的节点（否则会把用户正在编辑的值冲掉）。
   */
  let panelTemplate = root.querySelector<HTMLElement>(PANEL_SELECTOR)
  if (panelTemplate) panelTemplate = panelTemplate.cloneNode(true) as HTMLElement

  function panelHost(): HTMLElement | null {
    return root.querySelector<HTMLElement>('form.layui-form') ?? root.querySelector('form')
  }

  /** 取回可用的弹层节点；被 layui 摘走后从快照补一个。 */
  function ensurePanel(): HTMLElement | null {
    const existing = findPanel(root, PANEL_SELECTOR)
    if (existing) return existing
    const host = panelHost()
    if (!host || !panelTemplate) return null
    const fresh = panelTemplate.cloneNode(true) as HTMLElement
    host.appendChild(fresh)
    return fresh
  }

  /** 当前正在编辑的识别项（**闭包记住**，不依赖 `#disID` 的当前值）。 */
  let editingItemCode: string | null = null
  let layerIndex: number | null = null

  /**
   * 打开弹层。⚠️ **顺序是硬要求**：「先预填、再 open」。
   *
   * `layer.open({type:1, content: jq})` 会把 `.change` 节点**搬出 `<form>`** 塞进
   * `.layui-layer-content`；而 `form.val('dis', …)` 是按 `<form lay-filter="dis">` 的**作用域**
   * 找输入的 ⇒ 先 open 再预填，`form.val` 就找不到这些 `<input>`（打开是空的）。
   * `config-replica.openPanelLayer` 的 `end` 回调负责关闭后把节点放回，否则第二次打不开。
   */
  function openForm(row: RecognitionRow): void {
    editingItemCode = row.itemCode
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    const panel = ensurePanel()
    if (!panel) {
      // 节点与快照都没有：只提示，不假装打开了（绝不静默）。
      msg('弹窗内容缺失，请联系管理员')
      return
    }
    ;(form.val as (filter: string, data: Record<string, unknown>) => void)(FORM_FILTER, {
      key: row.disKey,
      disID: row.itemCode,
    })
    ;(form.render as () => void)()
    layerIndex = openPanelLayer(root, layui, layer, dialogOptions(row, panel), PANEL_SELECTOR)
  }

  /**
   * 保存（对标 `form.on('submit(savebtn)')`）。
   *
   * ⚠️ **必须从 DOM 读**：`layer.open({type:1})` 把 `.change` 节点搬出 `<form>` 之后，
   * 按钮的 `parents('form')` 是空的 ⇒ layui 的 `data.field` 是 `{}`（读码：
   * `public/layui/layui.js` 的 `t.prototype.submit`：`n = a.parents(h).eq(0)`）。
   * 对照先例：`system-sourelist.ts::submit` 同样从面板 DOM 读（它也不读 `data.field`）。
   */
  async function submit(): Promise<void> {
    const panel = findPanel(root, PANEL_SELECTOR)
    const keywords = panel?.querySelector<HTMLInputElement>('[name="key"]')?.value ?? ''
    // `editingItemCode` 是**当前行**（不是 `#disID` 的字符串）——判据用「有没有这一行」，不用真值。
    if (editingItemCode === null) return
    try {
      await updateRecognitionKeywords(editingItemCode, keywords)
    } catch (e) {
      // 失败**原样**显示后端 `msg`（业务码由 `api/http.ts` 的统一分支映射），
      // **不关弹层、不重载、不换文案、不吞错**。
      msg(errText(e))
      return
    }
    // 成功：关弹窗 + 刷新列表（后端成功 `msg` 是空串 ⇒ 用前端文案）。
    if (layerIndex !== null) {
      ;(layer.close as (i: number) => void)(layerIndex)
      layerIndex = null
    }
    editingItemCode = null
    reload()
    msg('保存成功')
  }

  /** 取数：`fetchRecognitionSettings()` → `table.reloadData(id, {data})`（**不给 layui 传 `url`**）。 */
  function reload(): void {
    void fetchRecognitionSettings().then(
      (data) => {
        ;(table.reloadData as (id: string, o: unknown) => void)(TABLE_ID, {
          data: data.items.map(toRow),
        })
      },
      (e: unknown) => msg(errText(e)),
    )
  }

  /* ── 表格 ──
   *
   * `data: []` 先渲染一张空表：`reloadData` 按实例 id 查表，不先 `render` 会静默失败
   * （`config-replica.ts:117-121` 同因）。**不传 `toolbar`**：对标这页没有表格工具栏
   * （证据件里 `.layui-btn-container` 数为 0）；`defaultToolbar` 是**空数组**（不是 `['filter']`）。
   */
  ;(table.render as (o: unknown) => void)({
    elem: TABLE_ELEM,
    id: TABLE_ID,
    data: [],
    // ⛔ 不传 `toolbar`（对标没有工具栏）。
    defaultToolbar: [],
    // 对标 `page:false`（`autodiscernset.js` L47）：一次取全，没有分页控件。
    page: false,
    height: 'full',
    cellMinWidth: 100,
    cols: [buildCols()],
    text: { none: '暂无数据' },
    done: () => {
      // 表格每次重绘都会重建那一带：把弹层节点的快照留在手里，重载后 `ensurePanel()` 才有
      // 干净副本可用（见其注释）。
      const current = root.querySelector<HTMLElement>(PANEL_SELECTOR)
      if (current) panelTemplate = current.cloneNode(true) as HTMLElement
      ;(form.render as () => void)()
    },
  })

  /* ── 交互：行内「修改关键词」/ 第一列蓝字 / 保存 ── */
  ;(table.on as (e: string, cb: (obj: unknown) => void) => void)(
    `tool(${TABLE_ID})`,
    (obj: unknown) => {
      const typed = obj as { event?: string; data?: RecognitionRow }
      if (typed.event !== 'submitEdit' || !typed.data) return
      openForm(typed.data)
    },
  )
  // 第一列蓝字（对标 templet 里的 `.editrow`）——委托点击，另一条开弹层的路。
  const onRowLinkClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    const link = target?.closest?.<HTMLElement>(EDITROW_SELECTOR) ?? null
    if (!link) return
    event.preventDefault()
    openForm({
      itemCode: link.dataset.disid ?? '',
      typeName: link.dataset.typename ?? '',
      disKey: link.dataset.diskey ?? '',
    })
  }
  root.addEventListener('click', onRowLinkClick)
  // 保存按钮（对标 `lay-submit lay-filter="savebtn"`，证据件 L106）。
  ;(form.on as (e: string, cb: () => void) => void)('submit(savebtn)', () => {
    void submit()
  })

  reload()

  return () => {
    root.removeEventListener('click', onRowLinkClick)
    if (layerIndex !== null) (layer.close as (i: number) => void)(layerIndex)
    root.innerHTML = ''
  }
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountRecognitionPage(root, ctx)
}

export default { mount } satisfies IslandPage
