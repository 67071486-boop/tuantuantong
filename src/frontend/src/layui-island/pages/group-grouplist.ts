/**
 * 【排团管理（旧版）】页的 **layui 复刻页**（对标物 `/Group/grouplist`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（筛选栏 5 个控件 + 工具栏 5 颗按钮 + 表格 + 3 个 `display:none` 弹层）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/group-grouplist.html`
 *    （源 `.tools/recon/suqing1/html/Group__grouplist.html` L24-222）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Group/grouplist.js` **L119-215**
 *  - 工具栏 / 行内按钮 → 同上 HTML 的 `#toolbarDemo`（L159-167）、`#baroperating`（L168-171）
 *
 * ## 为什么自己 render 表（共享渲染器的能力缺口，**不改共享渲染器**）
 *
 * `ArtifactCol` 已支持 `type`（`artifactPage.ts` L28-54），但**仍缺 `cols[].toolbar` / `cols[].edit`**：
 * 本页末列是对标的 `toolbar:'#baroperating'`（`grouplist.js` L214），交给 `tables` spec 会
 * **静默丢掉行内按钮模板**（空表下看不出来，与 `type` 漏写同一类陷阱），故本页仍在 `mount` 里
 * 自己 `table.render`。被同一缺口挡住的还有 `group-addeditgroup` / `group-grouplist2025`；
 * 本组另外两张（`group-mygroup` / `group-grouplist2020#orderList`）没有 `toolbar` 列，已走 spec。
 * （`page` / `defaultToolbar` / `cellMinWidth` 主会话已补进 `ArtifactTable`，本页没用过 `reload` 补丁。）
 * 对标这页是 `defaultToolbar:['filter']`（L118，第二次赋值生效）、
 * `page:true, limit:20, limits:[10,20,30,40,50,80,100,150,200]`（L216-218，**9 项逐字**）、
 * `height:'full-70'`（L114）。
 *
 * 对标 `#baroperating` 里两颗按钮是 **`<a>` + `layui-icon-edit`/`layui-icon-delete`**（真 layui 字形），
 * 且 `style=""` 空值（拿 CSS 默认的 `margin-left:5px`，来自页内 `.layui-btn + .layui-btn`）——
 * 模板在证据件里逐字保留，这里不重写。
 *
 * ## `xm-select`：接共享 `initFormSelects`
 *
 * 「团队指挥」是 `<select name="leader" id="leader" xm-select-radio lay-search="" xm-select="leader"
 * … xm-select-search="">`（HTML L121）——对标由 **formSelects-v4** 渲染（单选 + 可搜索），
 * 入口见 `@/layui/vendor`，兄弟页已在用（见 `pages/group-mygroup.ts` 的同一段）。
 * **不接**时本页实测 struct 6 / style 7 条差异（`.tools/parity-group.json`，全在
 * `sections.filterControls` / `sections.filterLabels` 的位置与宽度上：对标多出 `<input name="leader">`
 * （w=142）与一个 w=50 的搜索框，后续控件整体右移）；**接上**后为 **差异 0**
 * （`.tools/parity-group-xm.json`，6 页全 PASS）。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'
import { initFormSelects } from '@/layui/vendor'

const ARTIFACT_URL = 'replica/group-grouplist.html'

/** 对标 `table.render` 的 `elem` / `id`（`grouplist.js` L113）。 */
const TABLE_ELEM = '#tableList'

type Col = Record<string, unknown>

/** 表头（逐条照抄 `grouplist.js` L119-215）。 */
function buildCols(): Col[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'taskId', title: '排团编号', width: 70, fixed: 'left' },
    { field: 'taskName', title: '排团名称', width: 150 },
    { field: 'proJectName', title: '项目/副本名称', width: 150 },
    { field: 'gameArea', title: '游戏区服/阵营', width: 150 },
    { field: 'leaderUserName', title: '团队指挥', width: 90 },
    { field: 'startTime', title: '开团时间', width: 130 },
    { field: 'endTime', title: '完成时间', width: 90 },
    { field: 'taskStatus', title: '进度', width: 70, align: 'center' },
    { field: 'difficulty', title: '排团标识', width: 80 },
    { field: 'gameAccCount', title: '配号数', width: 80, align: 'center' },
    { field: 'postionAmount', title: '每坑价格', width: 80, align: 'center' },
    { field: 'orderId', title: '排序(越大越前)', width: 100, align: 'center', edit: true },
    { field: 'remark', title: '备注', unresize: true },
    // 对标 L214 写 `fixed: ''` = **不固定**；`unresize:true` 生效。
    { title: '操作', width: 110, toolbar: '#baroperating', unresize: true },
  ]
}

export const COLS_FOR_TEST: Col[] = buildCols()

const base = artifactPage({ url: ARTIFACT_URL })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Group__grouplist.html` L5）。
    document.title = '排团管理'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['form', 'table'])
    ;(layui.table as unknown as { render: (o: unknown) => void }).render({
      elem: TABLE_ELEM,
      data: [],
      // 对标 L114：`height:'full-70'`（视口 1000 → 930，见 `.tools/parity-all.json` 的对标观测）。
      height: 'full-70',
      toolbar: '#toolbarDemo',
      // 对标 L118 的第二次赋值生效。
      defaultToolbar: ['filter'],
      page: { limit: 20, limits: [10, 20, 30, 40, 50, 80, 100, 150, 200] },
      cols: [buildCols()],
      text: { none: '暂无数据' },
      done: () => (layui.form as unknown as { render: () => void }).render(),
    })
    ;(layui.form as unknown as { render: () => void }).render()
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
