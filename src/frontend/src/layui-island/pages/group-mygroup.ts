/**
 * 【我的排团】页的 **layui 复刻页**（对标物 `/Group/mygroup`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（筛选栏 3 个控件 + 工具栏 1 颗按钮 + 表格）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/group-mygroup.html`
 *    （源 `.tools/recon/suqing1/html/Group__mygroup.html` L24-137；PII 无、laytpl 无）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Group/mygroup.js` **L33-106**
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L131-135）
 *
 * ## 本页**完全走共享 `artifactPage` 的 `tables` spec**（没有自己 `table.render`）
 *
 * 本组 5 张表里只有这张与 `group-grouplist2020#orderList` 能被 spec 完整表达（见下），
 * 所以只有它们用 spec；另 3 张（`group-addeditgroup` / `group-grouplist` /
 * `group-grouplist2025`）的「操作」列带 `cols[].toolbar`，`ArtifactCol` 还没有这个字段，
 * 用 spec 会**静默丢掉行内按钮模板**，故仍在自己 `mount` 里 render（各页顶部有说明）。
 *
 * spec 用到的三个新字段（主会话 2026-09-20 扩展，`artifactPage.ts` L56-84）：
 *  - `type:'checkbox'` ← 对标 L34 `{ type:'checkbox', fixed:'left', width:30 }`。
 *    ⚠️ 漏了它表头勾选框会**静默消失**且 `parity.mjs` 不报（它只比表头文案+宽度）。
 *  - `defaultToolbar: ['filter']` ← 对标在同一 `render` 对象里写了两次
 *    （L30 `[]` 在前、**L32 `['filter']` 生效**）→ 表格右上角有「筛选列」图标。
 *  - `page: false` ← 对标**全文没有 `page`/`limit`/`limits`**（`grep -n "page\|limit" mygroup.js`
 *    0 命中；`layui.js` 的 table 默认配置里根本没有 `page` 键，`if(i.page)` 为假）。
 *    实测复核（read-only GET 对标页）：`.layui-laypage` 不存在、`.layui-table-page` 带 `layui-hide`
 *    → **确认 `page:false`**（主会话 2026-09-20 的判据 ②）。既然不分页，`limits` 对渲染无影响。
 *
 * ## `xm-select`：接共享 `initFormSelects`
 *
 * 筛选栏的「排团状态」是 `<select name="taskSta" xm-select="taskSta" …>`（HTML L108）——
 * 对标由 **formSelects-v4** 渲染（多出 `<input name="taskSta">` 与一个 w=140 的渲染位）。
 * 资产与初始化入口见 `@/layui/vendor`，兄弟页 `system-user-list` / `gameaccounts-*` 等 8 页已在用。
 * `bootstrapIsland()`（`artifactPage.mount` 内部第一句）已 `await ensureBenchmarkVendor()`，
 * 所以这里 DOM 注入完直接调即可；插件会给原 `<select>` 打 `lay-ignore`，后续 `form.render()` 不再动它。
 * **不接**时本页实测 struct 5 / style 4 条差异（`.tools/parity-group.json`，全在 `sections.filterControls`）；
 * **接上**后为 **差异 0**（`.tools/parity-group-xm.json`，6 页全 PASS）。
 */
import { artifactPage, type ArtifactCol } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'
import { initFormSelects } from '@/layui/vendor'

/** 证据件（对标页 `<style>` + body 标记，零转义，可直接与对标原文逐行对拍）。 */
const ARTIFACT_URL = 'replica/group-mygroup.html'

/** 对标 `table.render` 的 `elem`（`mygroup.js` L28；HTML L130 的 `id="tableList"`）。 */
const TABLE_ELEM = '#tableList'

/** 表头（逐条照抄 `mygroup.js` L33-106；`sort:false` / `unresize:false` 是 layui 默认值，不写）。 */
export function buildCols(): ArtifactCol[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'taskId', title: '排团编号', width: 80, unresize: true },
    { field: 'taskName', title: '排团名称', width: 200 },
    { field: 'gameArea', title: '游戏区服/阵营', width: 300, unresize: true },
    { field: 'leaderUserName', title: '团队指挥', width: 100 },
    { field: 'startTime', title: '开始时间', width: 100, align: 'center' },
    { field: 'endTime', title: '完成时间', width: 100, align: 'center' },
    { field: 'taskStatus', title: '进度', width: 80, align: 'center' },
    { field: 'difficulty', title: '排团标识', width: 100 },
    { field: 'gameAccCount', title: '配号数', width: 100 },
    { field: 'remark', title: '备注', unresize: true },
  ]
}

/** 导出给单测对拍表头序列（与 `mygroup.js` 的 `cols` 一致）。 */
export const COLS_FOR_TEST: ArtifactCol[] = buildCols()

const base = artifactPage({
  url: ARTIFACT_URL,
  tables: [
    {
      elem: TABLE_ELEM,
      toolbar: '#toolbarDemo',
      defaultToolbar: ['filter'],
      page: false,
      cols: buildCols(),
    },
  ],
})

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Group__mygroup.html` L5）。
    document.title = '我的排团'
    const unmount = await base.mount(root, ctx)
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
