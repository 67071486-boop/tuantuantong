/**
 * 【新版排团（2021 版）】页的 **layui 复刻页**（对标物 `/Group/grouplist2020`，**证据件渲染**）。
 *
 * 形状：**卡片看板页 + 3 张表**（筛选栏 6 个控件 + CD 日期 5 个 tag 复选框 + 空卡片列表 + 分页
 *      + `display:none` 的订单区/公开区/编辑弹层/自动配号/导入等一大票隐藏块）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/group-grouplist2020.html`
 *    （源 `.tools/recon/suqing1/html/Group__grouplist2020.html` L24-687）
 *  - 列定义：
 *      · `#orderList` → `.tools/recon/suqing1/assets/JS2019/Group/grouplist2020.js` **L1418-1537**
 *      · `#tableList_public` → 同文件 **L692-715**
 *      · `#moveOrderTable` → 同文件 **L1948-1968** 的 `tragTableCols`
 *
 * ## 三张表**只渲染 1 张**（这是对标初始态的真实渲染结果，不是我们偷懒）
 *
 * | 表 | 对标初始态 | 证据 |
 * |---|---|---|
 * | `#orderList` | **已渲染**（`loadSetPostionOrderList(0)` 在初始化时被调，`grouplist2020.js` **L62**），但父容器 `.orderDiv` 是 `display:none`（HTML L249） | 对标观测里恰好 1 个 `.layui-table-view`（main+left 两个表头组、th 全不可见） |
 * | `#tableList_public` | **未渲染** —— 只在点「公开坑位」时 `table.render`（L687，某个 `layer.open` 分支） | 同上（若渲染了会有第 2 个 view） |
 * | `#moveOrderTable` | **未渲染** —— 只在 `.tragBtn` 的 `mousedown` 里 `table.render`（L1984） | 同上；且容器 `.moveOrderTable` 是 `display:none`（页内样式 L95-102） |
 *
 * 所以 `#orderList` 走共享 `artifactPage` 的 `tables` spec；另两张**只保留标记**
 * （证据件里仍有它们的 `<table>`），配置写在 `buildPublicCols()` / `buildTragCols()` 里备查。
 * 这与 `pages/system-user-list.ts` 对 `#tableList2`/`#tableList3` 的处理口径一致。
 *
 * ## 4 处分页参数**逐处理取自页面 JS**（`grep -n "limits" grouplist2020.js` 全部命中如下）
 *
 * | 渲染物 | 出处 | 取值 |
 * |---|---|---|
 * | `#orderList` | L1538-1540 | `page:true, limit:20, limits:[10,20,30,40,50]`（**5 项**，逐字） |
 * | `#tableList_public` | L716-717 | `page:false`（`limit: publicList.length` 是**运行期值** → 不写，见下） |
 * | `#moveOrderTable` | L1992 | `page:false`，且 **`defaultToolbar: false`**（全批唯一的布尔 `false`，L1989） |
 * | `#pagediv`（卡片列表 `laypage`） | L261-265 | `limit: 8, limits:[2,4,6,8,10]`（**5 项**，逐字） |
 *
 * 另两处 `page:` 不是初始渲染：L174 是 `tableList.reload({page:{curr}})`、L482-487 是
 * 每张卡片里的 `table.init('taskTable_*', {limit:50, page:false})`（本页空卡片 ⇒ 不存在）。
 *
 * ## `type` 逐表核对（主会话 2026-09-20 的要求）
 *
 * `grep -n "type: *'" grouplist2020.js` 在 `cols` 里只有 **1 处**：L1420 的
 * `{ type:'checkbox', fixed:'left', width:30, hide:true }`（`#orderList` 首列）→ 已抄进 spec。
 * `#tableList_public`（L692-715）与 `#moveOrderTable`（`tragTableCols`，L1948-1968）**没有任何 type 列**。
 *
 * ## 两项**人工定值**（草稿 `notes` 已标出）
 *
 * 1. `#tableList_public` 的 `limit: publicList.length`（L717）是**运行期变量**（`publicList` 由
 *    L680-686 从接口数据筛出来）。因为同处 `page:false`（L716），`limit` 不参与分页渲染 ⇒
 *    **不写 `limit`**（等价 0）。**人工定值**。
 * 2. `#moveOrderTable` 的 `cols: tragTableCols`（L1991）是**运行期表达式**（L1948-1968 定义）。
 *    我**逐字核对过**静态解析：6 列 `recUser/打手`、`gameRoleName/角色名`、`gameJob/职业`、
 *    `gameGroup/阵营`、`itemRemark/装等`、`dlProject/需求`，**全部无 `width`**（自动列），
 *    与草稿 `tables[2]` **一致**（转换器静态解析正确）。
 *
 * ## `xm-select`：接共享 `initFormSelects`
 *
 * HTML 里 6 个 `xm-select`（`groupSta` / `taskSelect` / `orderSoures` / `gameJob` / `taskMangeUid` 等）。
 * 资产与入口见 `@/layui/vendor`，兄弟页已在用（见 `pages/group-mygroup.ts` 的同一段）。
 * **接上**后本页实测 **差异 0**（`.tools/parity-group-xm.json`，6 页全 PASS）。
 */
import { artifactPage, type ArtifactCol } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'
import { initFormSelects } from '@/layui/vendor'

const ARTIFACT_URL = 'replica/group-grouplist2020.html'

/** 对标 `table.render` 的 `elem`（`grouplist2020.js` L1413）。 */
const ORDER_TABLE_ELEM = '#orderList'
/** 卡片列表的分页容器（`grouplist2020.js` L262；HTML L246 的 `id="pagediv"`）。 */
const PAGEDIV_ELEM = 'pagediv'

/** 非 spec 的列（`buildPublicCols` / `buildTragCols` 只备查，可含 `toolbar` 等 spec 尚无的字段）。 */
type Col = Record<string, unknown>

/** `#orderList` 表头（逐条照抄 `grouplist2020.js` L1418-1537）。 */
export function buildOrderCols(): ArtifactCol[] {
  return [
    // L1420：`{ type:'checkbox', fixed:'left', width:30, hide:true }`（隐藏的勾选列）。
    { type: 'checkbox', fixed: 'left', width: 30, hide: true },
    { field: 'action', title: '操作', align: 'center', width: 75 },
    { field: 'orderIdView', title: '角色名', width: 120 },
    { field: 'gameGroup', title: '阵营', align: 'center', width: 45 },
    { field: 'project', title: '代练项目', width: 170 },
    { field: 'gameInfo', title: '角色信息', width: 180 },
    { field: 'orderSta', title: '订单状态', align: 'center', width: 60 },
    { field: 'orderSoure', title: '订单来源', align: 'center', width: 70 },
    { field: 'price', title: '接单价', align: 'center', width: 55 },
    { field: 'recMan', title: '打手', align: 'center', width: 75 },
    { field: 'orderRemark', title: '重要备注', width: 75 },
    { field: 'createTime', title: '录单时间', align: 'center', width: 75 },
  ]
  // ⚠️ 对标 L1423/L1461 写的是 `nresize: true`（**拼写错**，只有 `unresize` 生效），故不写。
}

export const COLS_FOR_TEST: ArtifactCol[] = buildOrderCols()

/**
 * `#tableList_public` 表头（逐字照抄 `grouplist2020.js` L692-715），本页**不 render**（见顶部表）。
 * 末列带 `toolbar:'#baroperating_public'`（L714）——`ArtifactCol` 尚无 `cols[].toolbar`，故这里用 `Col`。
 * 该表 `page:false`（L716）、`defaultToolbar: []`（L691）、`limit` 为运行期值（不写）。
 */
export function buildPublicCols(): Col[] {
  return [
    { field: 'gameJob', title: '职业', width: 80, align: 'left' },
    { field: 'itemRemark', title: '装等要求', width: 80, align: 'center' },
    { field: 'otherRemark', title: '天赋/盟约', width: 90, align: 'center' },
    { field: 'orderTips', title: '坑位备注', width: 140, align: 'left', edit: true },
    { field: 'isPublic', title: '是否已公开', width: 80, align: 'center' },
    { title: '操作', width: 60, toolbar: '#baroperating_public', unresize: true },
  ]
}

/**
 * `#moveOrderTable` 表头 = `tragTableCols`（L1948-1968 原文；**逐字核对过**，见顶部注释）。
 * 本页**不 render**；该表 `page:false`（L1992）、**`defaultToolbar: false`**（L1989，布尔值不是 `[]`）。
 */
export function buildTragCols(): Col[] {
  return [
    { field: 'recUser', title: '打手' },
    { field: 'gameRoleName', title: '角色名' },
    { field: 'gameJob', title: '职业' },
    { field: 'gameGroup', title: '阵营' },
    { field: 'itemRemark', title: '装等' },
    { field: 'dlProject', title: '需求' },
  ]
}

const base = artifactPage({
  url: ARTIFACT_URL,
  tables: [
    {
      elem: ORDER_TABLE_ELEM,
      toolbar: '#toolbarDemo_orderList',
      // 对标 L1417 的第二次赋值生效（L1415 的 `[]` 被覆盖）。
      defaultToolbar: ['filter'],
      page: { limit: 20, limits: [10, 20, 30, 40, 50] }, // 对标 L1538-1540（5 项，逐字）
      cols: buildOrderCols(),
    },
  ],
})

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Group__grouplist2020.html` L5）。
    document.title = '新版排团'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['laypage'])

    // 卡片列表分页（`grouplist2020.js` L259-271 的 `loadPage`）：无后端 ⇒ `count:0`（**不编造**）。
    ;(layui as unknown as { laypage: { render: (o: unknown) => void } }).laypage.render({
      elem: PAGEDIV_ELEM,
      count: 0,
      limit: 8,
      limits: [2, 4, 6, 8, 10],
      theme: '#FF5722',
    })

    // 统计文案：对标 `grouplist2020.js` **L305** 的
    // `$(".taskTotal").html("【搜索统计】本次搜索条件下共计" + data.count + '个排团')`。
    // 落点是 HTML L230 的 `<span class="taskTotal">`（在「新建一个团」右侧、筛选卡片内）。
    // 这句文案**占一行**，会把它下方的 `#pagediv` 往下推约 25px——不写分页的 y 就差 25px。
    // 无后端 ⇒ `data.count` 取 0，**只复刻模板不编造条数**。
    const taskTotal = root.querySelector<HTMLElement>('.taskTotal')
    if (taskTotal) taskTotal.textContent = '【搜索统计】本次搜索条件下共计0个排团'

    // 「本次搜索的缺口」引用块：对标 `grouplist2020.js` **L120-145** 的 `getCurrentWeekDayGrapJobData()`，
    // 由 `getTaskList()` 的 success 末尾调用（**L759**），落点是 HTML L234 的 `.layui-elem-quote.qjob`。
    // 空数据时它仍写入固定首行 `<p>本次搜索的缺口：</p>`（L132），使卡片高 57.2px（空着只有 32px）。
    // 同样只复刻模板、`data.msg` 为空 ⇒ 不追加缺口行。**不编造缺口。**
    const qjob = root.querySelector<HTMLElement>('.qjob')
    if (qjob) qjob.innerHTML = '<p>本次搜索的缺口：</p>'

    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
