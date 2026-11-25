/**
 * 【用户登录限制设置】页的 **layui 复刻页**（对标物 `/User/userloginrestrict`，**证据件渲染**）。
 *
 * 形状：**单表列表页 + 一个 `display:none` 的「限制IP地址」弹层**
 * （筛选栏 2 个控件 + 搜索按钮 + 工具栏 5 颗按钮 + 表格 + 行内操作按钮模板）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/user-userloginrestrict.html`
 *    （源 `.tools/recon/suqing1/html/User__userloginrestrict.html` 的 **L53-137**）
 *  - 列定义与 `table.render` 参数 → `.tools/recon/suqing1/assets/JS2019/User/UserLoginRestrict.js`
 *    **L37-119**：`elem:'#tableList'`、`toolbar:'#toolbarDemo'`（L41）、
 *    `defaultToolbar: []`（L40）后又写 `defaultToolbar: ['filter']`（L42，**后者生效**）、
 *    `page: true`（L113）/ `limit: 50`（L114）/ `limits: [10,20,30,40,50]`（L115）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（**L108-118**）；
 *    行内按钮 → `#baroperating`（**L119-121**）；开关列模板 → `#switchTpl1`（**L105-107**）
 *
 * 三处「不照抄」的对标缺陷（`impl-brief.md` §「不要照抄的对标缺陷」）：
 *  - `defaultToolbar` 写两次 ⇒ 取**后写生效**的 `['filter']`；
 *  - `IP范围` 列写的是 **`minwidth: 120`（小写 w）**，layui 只认 `minWidth` ⇒ 该属性**无效**，
 *    这一列在对标眼里是**自动列**。我们**不写**这个拼错的属性、也不「修好」成 `minWidth`，
 *    只复刻它的渲染结果（自动列）。实测对标：`IP范围` 渲染宽 **374**（= 1649 − 其余定宽列之和），
 *    其余列 = 声明值 + 1px。`UserLoginRestrict.js` **L97**。
 *  - `Status` 列的 `title` 里带着对标自己的 `<span lay-tips="…">`（L100）。`lay-tips` 是
 *    对标 layuiadmin 扩展的属性、我们 vendored 的 layui 里没有（`grep -c lay-tips` = 0），
 *    但**标记逐字保留**：layui 用 `{{- item2.title }}`（**不转义**）把它写进表头
 *    （vendored `public/layui/layui.js` offset 276366），两侧 DOM 因此一致。
 *
 * ⚠️ **`select[xm-select="userList"]` 必须交给对标的 formSelects 渲染**：对标侧实测
 * `select` 被 `display:none`、旁边多出 `.xm-select-parent`（3 个可见 `input`：隐藏值域 w=320 /
 * 可搜索输入 w=50 / 只读显示框 w=318）——不调 `initFormSelects` 我们这边就只有一个 `<select>`，
 * 探针的「筛选控件」会多一条、x/w 全错。`artifactPage` 不做这件事（它只调 `form.render`），
 * 所以在本页 `mount` 里补一次（与 `system-user-list.ts` 同一手法）。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「用户登录限制设置」（`User__userloginrestrict.html` **L5**）。
 */
import { artifactPage } from '@/layui/artifactPage'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'
import { initFormSelects } from '@/layui/vendor'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/user-userloginrestrict.html'
/** 对标 `page.limits`（`UserLoginRestrict.js` L115：`limits: [10, 20, 30, 40, 50]`）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50]

/**
 * 表头（逐条照抄 `UserLoginRestrict.js` **L43-112** 的 `cols[0]`，共 11 列含勾选列）。
 *
 * 勾选列的 `type` **必须显式声明**（`ArtifactCol.type`）——漏了它整列会静默变成普通列
 * （表头勾选框消失、宽度被二次分配，而 `parity` 的空文案特殊列不报差异）。
 * `sort` 本页一处都没写；`unresize` 标 `true` 的共 2 处（勾选列无此属性、`操作` 列 L111）。
 */
export const COLS: ArtifactCol[] = [
  { type: 'checkbox', fixed: 'left', width: 30 },
  { field: 'UserId', title: '系统用户', width: 200, align: 'center' },
  // 以下 4 列的 `templet` 都是**运行期拼接**（周期段/时间段/限制方式，L49-95），
  // 空表下不渲染；`artifactPage` 的 `buildCol` 会给它们留空模板。
  { field: 'weekDay', title: '限制周期段', width: 200, align: 'center' },
  { field: 'RestrictTimeS', title: '限制时间段', width: 200, align: 'center' },
  { field: 'RestrictType', title: '限制方式', width: 200, align: 'center' },
  // ⚠️ 对标写的是 `minwidth: 120`（小写 w，**无效**，L97）⇒ 不写任何宽度，保持自动列。
  { field: 'IPWhitelist', title: 'IP范围', align: 'center' },
  // `title` 里的 `<span lay-tips=…>` 逐字照抄（L100）；`templet:'#switchTpl1'` 换成 `switchText`
  // ——`ArtifactCol` 没有「模板选择器」这一项，`switchText` 是它提供的等价声明（空表下不渲染，
  // 只作备查）。这一点写进了交付报告。
  {
    field: 'Status',
    title: '<span lay-tips="绿色代表这个规则状态开启, 符合规则条件的用户才能登录。">规则状态</span>',
    width: 75,
    align: 'center',
    switchText: '生效|禁用',
  },
  { field: 'CreateTime', title: '创建时间', width: 100, align: 'center' },
  { field: 'UpdateTime', title: '更新时间', width: 100, align: 'center' },
  { field: 'LastModifyUserName', title: '修改人名称', width: 75, align: 'center' },
  // 操作列：`fixed:''`（= 不固定）是 layui 默认，不写；`toolbar` 少了它这列会静默变空列。
  { title: '操作', width: 85, toolbar: '#baroperating', unresize: true },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  toolbar: '#toolbarDemo',
  // 对标同对象里 `defaultToolbar` 写了两次（L40 `[]` / L42 `['filter']`），第二次生效。
  defaultToolbar: ['filter'],
  // 对标 L113-115：`page: true` / `limit: 50` / `limits: [10,20,30,40,50]`。
  page: { limit: 50, limits: PAGE_LIMITS },
  cols: COLS,
}

const base = artifactPage({ url: ARTIFACT_URL, tables: [TABLE] })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`User__userloginrestrict.html` L5）。
    document.title = '用户登录限制设置'
    const unmount = await base.mount(root, ctx)
    // 证据件已进 DOM 且 vendor 已就绪（`bootstrapIsland()` 里 `await ensureBenchmarkVendor()`），
    // 这里把 `select[xm-select]` 交给 formSelects（幂等：已渲染出 `dl[xid]` 就跳过）。
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
