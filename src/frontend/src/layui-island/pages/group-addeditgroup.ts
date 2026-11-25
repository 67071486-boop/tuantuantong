/**
 * 【新增/编辑排团】页的 **layui 复刻页**（对标物 `/Group/addeditgroup`，**证据件渲染**）。
 *
 * 形状：**表单页**（9 个表单项 + 职业快捷按钮 4 列 + 一个表格 + 3 个 `display:none` 弹层）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/group-addeditgroup.html`
 *    （源 `.tools/recon/suqing1/html/Group__addeditgroup.html` L24-320）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Group/addeditgroup.js` **L165-215**
 *  - 行内按钮模板 → 同上 HTML 的 `#baroperating`（L262-265）
 *
 * ## 三处「运行期取值」的人工定值（`impl-brief.md` §1「运行期取值」条）
 *
 * 1. `#tableList` 的 `limit: selectJobData.length`（`addeditgroup.js` L214）是**运行期变量**，
 *    新建态 `selectJobData = []`（L22）→ 长度为 0。因为同处 `page:false`（L213），`limit` 不参与
 *    分页渲染，这里按 0 处理（也**不写 `limit`**，与 `page:false` 等价）。**人工定值**。
 * 2. `在小程序上卖价` 列的 `hide: wechatApp=='Y'?false:true`（L194）是**运行期表达式**；
 *    `wechatApp` 取自隐藏域 `<input id="wechatApp" value="N">`（HTML L277）→ `'N' != 'Y'` → **`hide:true`**。
 *    **人工定值：隐藏**。证据：`Group__addeditgroup.html` L277。
 * 3. `height:'full'`（L167）是字面量，照抄。
 *
 * ## 一处**行为**复刻：`defGame` 不含「魔兽」时隐藏 `.jobDiv`
 *
 * `addeditgroup.js` L30-32：`if (defGame.indexOf('魔兽') < 0) { $('.jobDiv').hide(); }`；
 * `defGame` 取自隐藏域 `<input id="defGame" />`（HTML L319，**新建态为空**）→ 空串 `.indexOf('魔兽') = -1`
 * → **`.jobDiv` 隐藏**。这就是对标观测里 `#tableList` 的 `.layui-table-view` 包围盒全为 0
 * （`.tools/parity-all.json` 的 `group-addeditgroup.bench.tables[0].box`）的原因。
 * 我们照渲染结果复刻：mount 时把 `.jobDiv`（HTML L145/L158/L260 三处）全部置 `display:none`，
 * **在 `table.render` 之前**（对标也是在 render 之前 hide 的）。
 *
 * ## 为什么自己 render 表
 *
 * `ArtifactCol` 已支持 `type`，但**仍缺 `cols[].toolbar`**：本页末列是对标的
 * `toolbar:'#baroperating'`（L211），交给 `tables` spec 会**静默丢掉行内按钮模板**
 * （空表下看不出来）——同组被它挡住的还有 `group-grouplist` / `group-grouplist2025`；
 * 另两张无 `toolbar` 列的表已走 spec（见 `pages/group-mygroup.ts` 顶部）。
 * 对标这页是 `page:false`（L213）、`defaultToolbar: []`（L169，只赋值一次
 * → **没有**右上角「筛选列」图标，与同批其它页不同）、`limit: selectJobData.length`（L214，运行期=0）。
 * 本页自己 render 最省事（主会话已给 `ArtifactTable` 补 `page`/`defaultToolbar`，本页没用过 `reload` 补丁）。
 *
 * ## `xm-select`：接共享 `initFormSelects`
 *
 * 「项目」（`#projectSelect`，HTML L108，带 `xm-select-radio xm-select-create xm-select-search`）
 * 与「指定团长」（`#leader`，HTML L124，带 `xm-select-radio xm-select-search`）都是 **formSelects-v4**
 * 控件；入口见 `@/layui/vendor`，兄弟页 `system-user-list` / `gameaccounts-*` 等 8 页已在用。
 * **不接**时本页实测 struct 10 / style 3 条差异（`.tools/parity-group.json`，全在 `sections.filterControls`：
 * 对标 13 个可见控件 vs 我们 9 个）；**接上**后为 **差异 0**（`.tools/parity-group-xm.json`，6 页全 PASS）。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'
import { initFormSelects } from '@/layui/vendor'

const ARTIFACT_URL = 'replica/group-addeditgroup.html'

/** 对标 `table.render` 的 `elem` / `id`（`addeditgroup.js` L166）。 */
const TABLE_ELEM = '#tableList'

/** 隐藏域的 id：`defGame`（HTML L319）。 */
const DEF_GAME_ID = 'defGame'
/** 对标 L31 隐藏选择器（HTML 里 L145/L158/L260 三处带这个 class）。 */
const JOB_DIV_SELECTOR = '.jobDiv'

type Col = Record<string, unknown>

/** 表头（逐条照抄 `addeditgroup.js` L170-212）。 */
function buildCols(): Col[] {
  return [
    { field: 'index', title: '序号', width: 45, align: 'center' },
    { field: 'jobName', title: '坑位职业', width: 80, align: 'left' },
    { field: 'needItemLevel', title: '最低装等要求数', width: 110, align: 'center', edit: true },
    { field: 'gameTf', title: '天赋/盟约', width: 90, align: 'center', edit: true },
    // 见顶部注释 2：`wechatApp == 'N'` → `hide:true`（人工定值）。
    { field: 'postionPrice', title: '在小程序上卖价', width: 100, align: 'center', edit: true, hide: true },
    { field: 'remark', title: '职业备注', width: 120, align: 'left', edit: true },
    { field: 'userName', title: '坑位打手', width: 100, align: 'center' },
    // 对标 L211 写 `fixed: ''` = 不固定。
    { title: '操作', width: 200, toolbar: '#baroperating', unresize: true },
  ]
}

export const COLS_FOR_TEST: Col[] = buildCols()

const base = artifactPage({ url: ARTIFACT_URL })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Group__addeditgroup.html` L5）。
    document.title = '新增编辑排团'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['form', 'table'])

    // 复刻 `addeditgroup.js` L30-32（见顶部注释）：新建态 `#defGame` 为空 → 隐藏 `.jobDiv`。
    const defGame = (root.querySelector(`#${DEF_GAME_ID}`) as HTMLInputElement | null)?.value ?? ''
    if (defGame.indexOf('魔兽') < 0) {
      root.querySelectorAll<HTMLElement>(JOB_DIV_SELECTOR).forEach((el) => {
        el.style.display = 'none'
      })
    }

    ;(layui.table as unknown as { render: (o: unknown) => void }).render({
      elem: TABLE_ELEM,
      data: [],
      height: 'full',
      defaultToolbar: [],
      // 对标 L213：`page:false`（`limit` 因此不参与渲染，见顶部注释 1）。
      page: false,
      cols: [buildCols()],
      text: { none: '暂无数据' },
      done: () => (layui.form as unknown as { render: () => void }).render(),
    })
    ;(layui.form as unknown as { render: () => void }).render()
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
