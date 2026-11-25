/**
 * 【系统用户黑名单管理】页的 **layui 复刻页**（对标物 `/User/BackUserList`，**证据件渲染**）。
 *
 * 形状：**单表列表页 + 一个 `display:none` 的「新增/编辑黑名单」弹层**
 * （筛选栏 1 个控件 + 搜索按钮 + 工具栏 3 颗按钮 + 表格 + 行内操作按钮模板）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/user-backuserlist.html`
 *    （源 `.tools/recon/suqing1/html/User__BackUserList.html` 的 **L52-175**）
 *  - 列定义与 `table.render` 参数 → `.tools/recon/suqing1/assets/JS2019/User/BackUserList.js`
 *    **L30-83**：`elem:'#tableList'`、`toolbar:'#toolbarDemo'`（L34）、
 *    `defaultToolbar: []`（L33）后又写 `defaultToolbar: ['filter']`（L35，**后者生效**）、
 *    `page: true`（L77）/ `limit: 20`（L78）/ `limits: [10,20,30,40,50,80,100]`（L79）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（**L101-107**）；
 *    行内按钮 → `#baroperating`（**L108-110**）
 *
 * ⚠️ 两处「不照抄」的对标缺陷（`impl-brief.md` §「不要照抄的对标缺陷」）：
 *  - `defaultToolbar` 写两次 ⇒ 取**后写生效**的 `['filter']`；
 *  - 工具栏**三颗**按钮写的是**自闭合** `<i class="layui-icon layui-refresh" />`（L103-105，
 *    不是 `</i>`）。**照抄自闭合**：`<script type="text/html">` 里它是纯文本，但 layui
 *    `$(…).html(渲染结果)` 之后按 HTML 解析，`<i … />` 会被当成**未闭合的开始标签**，
 *    于是后面的文案变成 `<i>` 的子节点（继承 `.layui-icon` 的字体/字号 ⇒ 直接决定按钮宽度）。
 *    实测对标三颗按钮宽度 **64 / 124 / 40**；改成显式 `</i>` 会变宽——这正是
 *    `impl-brief.md` 里「把对标 templet 里的死代码当渲染结果」的反面：**自闭合在这里是活的**。
 *
 * ⚠️ `手机号` 列是对标唯一的 `fixed:'left'` 业务列（L39，`align:'left'`）。**逐字抄**。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「系统用户黑名单管理」（`User__BackUserList.html` **L5**）。
 */
import { artifactPage } from '@/layui/artifactPage'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/user-backuserlist.html'
/** 对标 `page.limits`（`BackUserList.js` L79：`limits: [10, 20, 30, 40, 50,80,100]`，7 档）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50, 80, 100]

/**
 * 表头（逐条照抄 `BackUserList.js` **L36-76** 的 `cols[0]`，共 12 列含勾选列）。
 *
 * 勾选列的 `type` **必须显式声明**（`ArtifactCol.type`）——漏了它整列会静默变成普通列
 * （表头勾选框消失、宽度被二次分配，而 `parity` 的空文案特殊列不报差异）。
 * `sort:false` / `unresize:false` 是 layui 默认值，不写；`unresize:true` 只有操作列（L75）。
 * 「原因或备注」**没有 `width`**（L58）⇒ 自动列，实测对标渲染宽 **338**（= 1649 − 其余定宽列之和）。
 */
export const COLS: ArtifactCol[] = [
  { type: 'checkbox', fixed: 'left', width: 30 },
  { field: 'moblie', title: '手机号', width: 150, fixed: 'left', align: 'left' },
  { field: 'ww', title: '微信号', width: 130, align: 'center' },
  { field: 'qq', title: 'QQ', width: 130, align: 'center' },
  { field: 'rname', title: '名称', width: 120, align: 'center' },
  { field: 'cid', title: '身份证号', width: 150, align: 'center' },
  { field: 'yy', title: 'YY号', width: 120, align: 'center' },
  { field: 'alipayacc', title: '收款账号', width: 150, align: 'center' },
  // `templet` 是运行期拼接的链接（L58-61），空表下不渲染；不写宽度 ⇒ 自动列。
  { field: 'remark', title: '原因或备注', align: 'left' },
  { field: 'lastTime', title: '加入时间', width: 150, align: 'center' },
  { field: 'addUserName', title: '添加人', width: 90, align: 'center' },
  // 操作列：`fixed:''`（= 不固定）是 layui 默认，不写；`toolbar` 少了它这列会静默变空列。
  { title: '操作', width: 80, toolbar: '#baroperating', unresize: true },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  toolbar: '#toolbarDemo',
  // 对标同对象里 `defaultToolbar` 写了两次（L33 `[]` / L35 `['filter']`），第二次生效。
  defaultToolbar: ['filter'],
  // 对标 L77-79：`page: true` / `limit: 20` / `limits: [10,20,30,40,50,80,100]`。
  page: { limit: 20, limits: PAGE_LIMITS },
  cols: COLS,
}

const base = artifactPage({ url: ARTIFACT_URL, tables: [TABLE] })

export default {
  mount: (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`User__BackUserList.html` L5）。
    document.title = '系统用户黑名单管理'
    return base.mount(root, ctx)
  },
} satisfies IslandPage
