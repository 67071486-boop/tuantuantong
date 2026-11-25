/**
 * 【签约管理】页的 **layui 复刻页**（对标物 `/User/UserContractSigning`，**证据件渲染**）。
 *
 * 形状：**单表列表页 + 一个 `display:none` 的「签约功能配置」弹层**
 * （筛选栏 5 个控件 + 搜索按钮 + 工具栏 6 颗按钮 + 表格，**没有行内操作列**）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/user-usercontractsigning.html`
 *    （源 `.tools/recon/suqing1/html/User__UserContractSigning.html` 的 **L53-244**）
 *  - 列定义与 `table.render` 参数 →
 *    `.tools/recon/suqing1/assets/JS2019/User/UserContractSigning.js` **L61-172**：
 *    `elem:'#tableList'`、`toolbar:'#toolbarDemo'`（L65）、`defaultToolbar: []`（L64）后
 *    又写 `defaultToolbar: ['filter']`（L66，**后者生效**）、`page: true`（L159）/
 *    `limit: 20`（L160）/ `limits: [20, 50, 100]`（L161）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（**L193-202**）
 *
 * ⚠️ 不照抄的对标缺陷（`impl-brief.md` §「不要照抄的对标缺陷」）：
 *  - `defaultToolbar` 写两次 ⇒ 取**后写生效**的 `['filter']`；
 *  - 对标自己的 `parseData`（L162-168）是被 `//` 整段注释掉的死代码，不抄；
 *  - 末尾那颗「批量删除」按钮的标记是 `type="button"lay-tips="…"`（**缺空格**，L200）
 *    ——证据件里**逐字保留**（属性解析结果不受影响，改它反而偏离对标原文）。
 *
 * ⚠️ 最后一列（`操作`，L131-156）在**对标**里是 `templet` 函数（按行状态拼「关联用户 /
 * 重新发起 / 查看合同 / 解除合同 / 撤回签约」等按钮），**不是** `toolbar` 模板选择器。
 * 本页没有 `#baroperating`，所以**不要**给它写 `toolbar`（写了会凭空多出一个不存在的模板）。
 *
 * ⚠️ PII：`#sid`（**已清空：签约会话 ID**，对标原文 **L244**；证据件里已改成 `value=""`
 * 并留了行内注释）。
 * 两个日期输入框的 `value="2026-08-20"` / `"2026-09-20"` 是对标**服务端渲染**的默认区间
 * （页内 JS 只把它们交给 laydate，不改值），逐字保留。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「签约管理」（`User__UserContractSigning.html` **L5**）。
 */
import { artifactPage } from '@/layui/artifactPage'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/user-usercontractsigning.html'
/** 对标 `page.limits`（`UserContractSigning.js` L161：`limits: [20, 50, 100]`）。 */
export const PAGE_LIMITS = [20, 50, 100]

/**
 * 表头（逐条照抄 `UserContractSigning.js` **L67-158** 的 `cols[0]`，共 10 列含勾选列）。
 *
 * 勾选列的 `type` **必须显式声明**（`ArtifactCol.type`）——漏了它整列会静默变成普通列
 * （表头勾选框消失、宽度被二次分配，而 `parity` 的空文案特殊列不报差异）。
 * `sort:false` / `unresize:false` 是 layui 默认值，不写；本页**一处 `unresize:true` 都没有**。
 * `系统用户名称`（L82）与 `操作`（L131）**没有 `unresize`/`sort`**，但都有 `width`；
 * `操作` 那一列的 `templet` 是函数（运行期拼按钮）⇒ 空表下不渲染，也不写 `toolbar`。
 * 全部 10 列都有 `width`（无自动列）。
 */
export const COLS: ArtifactCol[] = [
  { type: 'checkbox', fixed: 'left', width: 30 },
  { field: 'name', title: '真实姓名', width: 150, align: 'center' },
  { field: 'idCadrNo', title: '身份证号', width: 145, align: 'center' },
  { field: 'mobile', title: '手机号', width: 130, align: 'center' },
  { field: 'userName', title: '系统用户名称', width: 160, align: 'center' },
  { field: 'age', title: '年龄', width: 60, align: 'center' },
  // `签约状态` 是对标用页内 `.lableb` 徽标按状态着色的 templet（L100-123），空表下不渲染。
  { field: 'status', title: '签约状态', width: 100, align: 'center' },
  { field: 'createTime', title: '邀请时间', width: 130, align: 'center' },
  { field: 'sigingCompleteTime', title: '签约时间', width: 130, align: 'center' },
  // ⚠️ 对标 `field` 与上一列**同名**（都是 `sigingCompleteTime`，L128 / L131）——照抄，不要「修好」。
  { field: 'sigingCompleteTime', title: '操作', width: 240, align: 'left' },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  toolbar: '#toolbarDemo',
  // 对标同对象里 `defaultToolbar` 写了两次（L64 `[]` / L66 `['filter']`），第二次生效。
  defaultToolbar: ['filter'],
  // 对标 L159-161：`page: true` / `limit: 20` / `limits: [20, 50, 100]`。
  page: { limit: 20, limits: PAGE_LIMITS },
  cols: COLS,
}

const base = artifactPage({ url: ARTIFACT_URL, tables: [TABLE] })

/**
 * 工具栏那颗「购买签约合同套餐包（剩余<span class="btnSpan"></span>次）」的套餐余量槽。
 *
 * 对标**模板里这个槽是空的**（`User__UserContractSigning.html` L198），数字由页面 JS 在
 * **运行期**用 ajax 结果填进去：
 * `UserContractSigning.js` **L34-47** `$.ajax({url:'/api/SignContract/GetSystemContractTotal'})
 * → success: $('.btnSpan').text(data.data)`。
 *
 * 我们**没有**这个接口（也**不发**任何写请求；这里的 GET 同样不发），所以槽位必须由本页给值：
 *  - 留空 → 按钮文案变成「购买签约合同套餐包（剩余次）」，探针报 **1 条 struct（文案）+ 3 条
 *    style（本按钮宽度 −6.7px、后面两颗按钮 x 各左移 6.7px）**；
 *  - 写 `—` → 文案仍然对不上，只是把「缺数字」换成「占位符」，4 条差异一条不少；
 *  - 写 `0` → 与对标**当期渲染值**逐字一致（实测对标就是 `（剩余0次）`）。
 *
 * 取 `0`：`FRONTEND-REPLICA-STANDARD.md` §10.7 明确允许「需要示例值对齐观感时用**假数据**」，
 * 而 `0` 是**不承载租户信息**的值。⚠️ 代价如实登记：它是对标**运行期**的数据（不是代码常量），
 * 若对标那个套餐余量不再是 0，这一格就会漂移——那时应改的是这一行常量，不是去改渲染器。
 */
const PACKAGE_REMAINING = '0'

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`User__UserContractSigning.html` L5）。
    document.title = '签约管理'
    const unmount = await base.mount(root, ctx)
    // 对标是 ajax 成功后写 `.btnSpan`（L44）；我们在挂载后写同一个槽（证据件本身保持
    // 与对标模板逐字一致——槽里本来就是空的）。
    const slot = root.querySelector<HTMLElement>('.btnSpan')
    if (slot) slot.textContent = PACKAGE_REMAINING
    return unmount
  },
} satisfies IslandPage
