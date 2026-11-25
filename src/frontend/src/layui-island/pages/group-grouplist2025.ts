/**
 * 【排团管理（2025 版）】页的 **layui 复刻页**（对标物 `/Group/grouplist2025`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（筛选栏 4 个控件 + 工具栏 6 颗按钮 + 表格 + 3 个 `display:none` 弹层）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/group-grouplist2025.html`
 *    （源 `.tools/recon/suqing1/html/Group__grouplist2025.html` L24-200）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Group/grouplist2025.js` **L119-194**
 *  - 工具栏 / 行内按钮 → 同上 HTML 的 `#toolbarDemo`（L136-145）、`#baroperating`（L146-149）
 *
 * ## 两处「对标原文照抄、但值很特殊」的地方
 *
 * 1. 工具栏第 1 颗是 `<a class="layui-btn **layui-btn-sm** layui-btn-danger addNewTask" lay-tips="点击新开一个排团">新建一个团</a>`
 *    （HTML L138）——**只有它是 `-sm`（30px）**，其余 5 颗是 `-xs`（22px）。照抄 class，别统一成 `-xs`
 *    （对标观测 `.tools/parity-all.json`：这颗 h=30/lh=30px，其余 h=22/lh=22px）。
 * 2. `缺口` 列是 `minWidth: 80`（**大写 W，有效**，`grouplist2025.js` L182）——与
 *    `pages/system-user-list.ts` 里那批小写 `minwidth`（无效 → 自动列）**不是一回事**，
 *    这里是真正的 `minWidth`，照抄。
 *
 * `gameName` 那个 `<select>` 在对标 HTML 里**本来就是空的**（L86-88，页面 JS 也没有填它），
 * 所以这里同样保留空壳——不是我们漏了游戏列表。
 *
 * ## 为什么自己 render 表
 *
 * `ArtifactCol` 已支持 `type`（首列 checkbox 已能表达），但**仍缺 `cols[].toolbar`**：本页末列是
 * 对标的 `toolbar:'#baroperating'`（`grouplist2025.js` L193），交给 `tables` spec 会静默丢掉
 * 行内按钮模板。同组 `group-addeditgroup` / `group-grouplist` 同样被挡；另两张无 `toolbar` 列的
 * 表已走 spec（见 `pages/group-mygroup.ts` 顶部）。本页没用过 `reload` 补丁。
 * 分页参数逐字取自 `grouplist2025.js` **L195-197**：`page:true, limit:20,
 * limits:[10,20,30,40,50,80,100,150,200]`（**9 项**）；`height:'full-70'` 取自 **L114**。
 * 本页 HTML **没有 `xm-select`**（`grep -c xm-select` = 0），故不调 `initFormSelects`。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'

const ARTIFACT_URL = 'replica/group-grouplist2025.html'

/** 对标 `table.render` 的 `elem` / `id`（`grouplist2025.js` L113）。 */
const TABLE_ELEM = '#tableList'

type Col = Record<string, unknown>

/** 表头（逐条照抄 `grouplist2025.js` L119-194）。 */
function buildCols(): Col[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'taskId', title: '排团编号', width: 70, fixed: 'left' },
    { field: 'taskName', title: '排团名称', width: 150 },
    { field: 'gameName', title: '游戏', width: 85 },
    { field: 'startTime', title: '开团时间', width: 90 },
    { field: 'endTime', title: '完成时间', width: 90 },
    { field: 'taskStatus', title: '进度', width: 70, align: 'center' },
    { field: 'totalCount', title: '坑位数', width: 65 },
    { field: 'gameAccCount', title: '已配号数', width: 80, align: 'center' },
    // `minWidth` 大写 W = **有效**（L182）；`align:'left'` 也是原文。
    { field: 'lackJobName', title: '缺口', minWidth: 80, align: 'left' },
    { field: 'orderId', title: '排序', width: 60, align: 'center', edit: true },
    { field: 'remark', title: '备注', unresize: true },
    { title: '操作', width: 110, toolbar: '#baroperating', unresize: true },
  ]
}

export const COLS_FOR_TEST: Col[] = buildCols()

const base = artifactPage({ url: ARTIFACT_URL })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Group__grouplist2025.html` L5）。
    document.title = '排团管理'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['form', 'table'])
    ;(layui.table as unknown as { render: (o: unknown) => void }).render({
      elem: TABLE_ELEM,
      data: [],
      // 对标 L114：`height:'full-70'`。
      height: 'full-70',
      toolbar: '#toolbarDemo',
      // 对标 L118 的第二次赋值生效。
      defaultToolbar: ['filter'],
      page: { limit: 20, limits: [10, 20, 30, 40, 50, 80, 100, 150, 200] }, // 对标 L195-197（9 项，逐字）
      cols: [buildCols()],
      text: { none: '暂无数据' },
      done: () => (layui.form as unknown as { render: () => void }).render(),
    })
    ;(layui.form as unknown as { render: () => void }).render()
    return unmount
  },
} satisfies IslandPage
