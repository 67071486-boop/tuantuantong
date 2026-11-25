/**
 * 【账号管理中心】页的 **layui 复刻页**（对标物 `/GameAccounts/accountlist`，**证据件渲染**）。
 *
 * 形状：**超大单表列表页**（28KB，全组最大）——
 * 筛选栏 16 个 label（15 个下拉 + 1 个日期区间 + 6 个文本框；另有**1 个 `layui-hide` 的「进度状态」**）
 * + 工具栏 **15 颗按钮**（全在 `#toolbarDemo` 里，带 `lay-tips`）
 * + 表格 + **5 个 `display:none` 弹层**（改使用状态 / 改用途类型 / 改进度状态 / 卖出 / 提号信息 / 导出）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/gameaccounts-accountlist.html`
 *    （源 `.tools/recon/suqing1/html/GameAccounts__accountlist.html` 的 L52-460）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/GameAccounts/accountlist.js` 的 `table.render`
 *    （`elem:'#tableList'`、`toolbar:'#toolbarDemo'`、
 *    `defaultToolbar:[]` 后又写 `defaultToolbar:['filter']`（**后者生效**）、
 *    `page:true`、`limit:20`、`limits:[10,20,30,40,50,100]`）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L279-298），`#total` 统计文案模板在 L297
 *
 * ## 表格：**只在这里**用本地 `table.render`（理由只剩一条：`#total` 的 `done` 回调）
 *
 * 共享渲染器已支持 `ArtifactTable.page` / `defaultToolbar` / `cellMinWidth`，本页**不需要**它们中
 * 任何一个作为自渲染理由；剩下的唯一缺口是 **`done` 回调**：对标在这里的 `done` 里
 * 1) 往 `#total` 写统计文案、2) 按游戏名 `display:none` 掉「阵营」列。`ArtifactTable` 没有
 * `done` 字段 ⇒ 本页保留本地 `renderTable()`（与同批 `pages/group-grouplist.ts` 同一做法），
 * 其余两页（`myaccountlist` / `gameitemlist`）已改回声明式。
 *
 * ## ⚠️ 2 个 `hide:true` 列**不显示**（照抄对标的实际渲染）
 *
 * `costPrice`（成本价，L273）与 `profit`（利润，L282）两列才是 `hide:true`；
 * `gameAcc`（游戏账号）**不是**——我第一版按草稿把它也写成 `hide:true`，
 * 探针立刻报「表头 20 vs 19、第 9 列起整列错位」。实测对标表头 20 个 `th`：
 * 勾选 + 19 个可见列，**含**「游戏账号」、**不含**「成本价 / 利润」。
 *
 * ## ⚠️ 3 个 `minWidth` 列与 1 个 `width < minWidth` 列
 *
 * - `gameTF`（当前信息1）`minWidth:115`、`otherLevel`（当前信息2）`minWidth:110`、
 *   `otherMark`（当前信息3）`minWidth:110`：**都没有 `width`** → 在 layui 眼里是自动列，
 *   由「表宽 − 定宽列之和」均分、再被各自 `minWidth` 兜底。实测 authored 一律 `60px`
 *   （layui 的 `width` 兜底是 `cellMinWidth:60`，`minWidth` 只在**收窄**时生效），
 *   渲染宽 `116 / 111 / 111`。**不补 `width`**（补了会撑破总宽）。
 * - `jiwei`（机位）`width:40` < `cellMinWidth:60` → layui 把它抬到 60（实测 `60px`/渲染 61）。
 *   照抄 `40`，让 layui 自己抬——写 60 会与作者的声明值对不上（探针会报 `authored` 差异）。
 *
 * ## ⚠️ 一个「源码看起来像错、其实不是」的点
 *
 * `gameRoleName` 那行列在产物 js 里是 `width:95`（无空格，`accountlist.js` L217）。
 * 用通用正则抓 `width\s*:` 时若要求冒号后必须有空格会漏掉它 → 会被误判成自动列。
 * 这里按原文 `95`。
 *
 * ## ⚠️ 「阵营」列按**实测隐藏**（对标 `done` 回调里把它 `display:none` 了）
 *
 * `accountlist.js` 的 `done` 回调末尾（L397-399）有一句
 * `if (gameName != '魔兽世界') { $("[data-field='gameGroup']").css('display','none') }`。
 * 实测（playwright 打开对标页 → 该列 `th` 的 `computed display` 是 `none`、`宽 0`）：
 * 默认游戏是「全部游戏」（`<option value="21714" selected>`），**所以这一列在对标上是隐藏的**。
 * 探针的 `vis()` 判据（`offsetParent !== null || getClientRects().length > 0`）会**保留**
 * `display:none` 的 `th`（`display:none` 的 `th` 仍有 client rects），所以差异表现得像
 * 「第 4 列文案对不上、后面整列错位」——但根因就是这一列的显隐。
 * ⇒ 这里按实测写 `hide: true`（`hide` 与对标 `display:none` 在表头渲染上等价）。
 *
 * ## ⚠️ 2 个 `xm-select` 之外的 13 个：用对标那份 vendor 库渲染（**不自造**）
 *
 * 本页筛选栏的 13 个下拉（`accType`/`gameArea`/`gameServer`/`gameGroup`/`recUser`/`useType`/
 * `inputOrderKefu` + 弹层里的 `useType2`，以及 `xm-select` 标记的其它项）由对标自带的
 * **formSelects v4** 渲染。仓库里已有对标那份**逐字节相同**的
 * `public/vendor/formselects-v4.js`（sha256 与 `/layuiadmin/lib/formSelects-v4.js` 实测一致），
 * 共享入口是 `@/layui/vendor` 的 `initFormSelects()`（`pages/system-user-list.ts` 已在用）。
 * ⇒ 本页照该既有做法调一次，**不另写多选控件**（`impl-brief.md` §「已知的可接受差异」那一条
 * 因此在本页不适用：能力是有的）。
 *
 * ⚠️ 工具栏「查看选号网址」按钮的 `data-content` / `data-clipboard-text` 里的**本租户 id
 * 已清空**（原值 `…/sellacc/?id=5819`，见证据件头部注释的 PII 处置）。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「账号管理中心」（`GameAccounts__accountlist.html` L5）。
 */
import { artifactPage } from '@/layui/artifactPage'
import { initFormSelects } from '@/layui/vendor'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/gameaccounts-accountlist.html'
/** 对标 `table.render` 的 `elem`（`accountlist.js`）。 */
export const TABLE_ELEM = '#tableList'
/** 对标 `page.limits`（`accountlist.js`，**6 档**，比 layui 默认多了 100）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 100]
/** 对标 `toolbar` 选择器（`accountlist.js`）。 */
export const TOOLBAR_ELEM = '#toolbarDemo'
/** 对标 `page.limit`（`accountlist.js`）。 */
export const PAGE_LIMIT = 20

type Col = Record<string, unknown>

/** 表头（逐条照抄 `accountlist.js` 的 `cols[0]`，共 22 列含勾选列）。 */
export function buildCols(): Col[] {
  return [
    // `{ type:'checkbox', fixed:'left', width:30 }`
    { type: 'checkbox', fixed: 'left', width: 30 },
    // 逐条照抄 L190-383。`align` 原文出现两次时（`center` 后又 `left`）以**后者**为准。
    { field: 'gameAccId', title: '编号', width: 70, align: 'left' },
    { field: 'sellTradeNo', title: '卖出/出租单号', width: 130, align: 'left' },
    { field: 'gameRoleName', title: '角色名', width: 95 },
    // `width:35` < `cellMinWidth:60` → 由 layui 抬到 60；`unresize:true`。
    // **`hide:true` 是按对标的实际渲染写的**（`done` 回调里对非「魔兽世界」把它 `display:none`），
    // 见文件头注释；不写就会在这一列开始整列错位。
    { field: 'gameGroup', title: '阵营', width: 35, align: 'center', hide: true, unresize: true },
    { field: 'gameInfo', title: '区服信息', width: 118 },
    // 自动列 + `minWidth:115`（**不补 width**，见文件头注释）
    { field: 'gameTF', title: '当前信息1', minWidth: 115 },
    // 自动列 + `minWidth:110`
    { field: 'otherLevel', title: '当前信息2', minWidth: 110, align: 'left' },
    // 自动列 + `minWidth:110`
    { field: 'otherMark', title: '当前信息3', minWidth: 110, align: 'left' },
    { field: 'Tips', title: '标签', width: 80 },
    // ⚠️ **不写 `hide`**：`accountlist.js` L267-271 这列**没有** `hide`（全文件只有 L273
    // `costPrice` 与 L282 `profit` 两处 `hide: true`），实测对标表头也有它（20 个 `th` 的第 9 个）。
    { field: 'gameAcc', title: '游戏账号', width: 80, align: 'center' },
    // `hide:true`
    { field: 'costPrice', title: '成本价', width: 60, align: 'center', hide: true },
    { field: 'sellPrice', title: '售价', width: 60, align: 'center' },
    // `hide:true`
    { field: 'profit', title: '利润', width: 60, align: 'center', hide: true },
    { field: 'accStatus', title: '账号状态', width: 65, align: 'center' },
    { field: 'accUseType', title: '用途类型', width: 65, align: 'center' },
    { field: 'recUserName', title: '接手人', width: 70, align: 'center' },
    { field: 'createTime', title: '入库时间', width: 85, align: 'center', sort: true },
    { field: 'tempDisableTime', title: '解/卖/租时', width: 85, align: 'center', sort: true },
    { field: 'img', title: '账号截图', width: 90, align: 'center' },
    { field: 'remark', title: '备注', width: 70 },
    { field: 'jiwei', title: '机位', width: 40 },
    // 对标唯一的 `fixed:'right'` 列；`unresize:true`
    { title: '操作', width: 92, align: 'left', fixed: 'right', unresize: true },
  ]
}

/** 表头的成品（`buildCols()` 的求值结果）——供测试与自检用。 */
export const COLS: Col[] = buildCols()

const base = artifactPage({ url: ARTIFACT_URL })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`GameAccounts__accountlist.html` L5）。
    document.title = '账号管理中心'
    const unmount = await base.mount(root, ctx)
    // 对标 `accountlist.js` 的 formSelects 渲染（13 个 `xm-select`，见文件头注释）。
    initFormSelects('select[xm-select]')
    const layui = await bootstrapIsland(['form', 'table'])
    ;(layui.table as unknown as { render: (o: unknown) => void }).render({
      elem: TABLE_ELEM,
      data: [],
      toolbar: TOOLBAR_ELEM,
      // 对标同对象里写了两次，第二次 `['filter']` 生效。
      defaultToolbar: ['filter'],
      page: { limit: PAGE_LIMIT, limits: PAGE_LIMITS },
      cols: [buildCols()],
      text: { none: '暂无数据' },
      done: () => {
        ;(layui.form as unknown as { render: () => void }).render()
        // 对标 `done` 回调里把统计文案写进 `#total`（`#toolbarDemo` 模板里那个
        // `<div style="color:#ff6a00" id="total">`）。文案骨架逐字照抄，**数字是数据**：
        // 我们恒空表、不发请求，故按对标的「0 条」形态给 0（探针比 `#total` 时会把数字
        // 抹成 `#` 再比骨架，见 `.tools/parity.mjs` 的 `tmpl()`）。
        const totalEl = document.getElementById('total')
        if (totalEl) {
          totalEl.textContent =
            '本次搜索条件下共计0条记录,总成本价0元，其中已卖出共计0元，共预计利润0元'
        }
      },
    })
    ;(layui.form as unknown as { render: () => void }).render()
    return unmount
  },
} satisfies IslandPage
