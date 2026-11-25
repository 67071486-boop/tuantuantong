/**
 * 【角色职位分组】页的 **layui 复刻页**（对标物 `/User/usergroup`，**证据件渲染**）。
 *
 * 与 `/System/roleauthority`（角色职位权限）成对：那页管「一个角色职位有哪些权限」，
 * 这页管「一个角色分组包含哪些角色职位」。两页形状也像（单表 + 行内操作按钮）。
 *
 * 形状：**单表列表页**（无筛选栏、无工具栏，只有 1 张表 + 1 个行内操作按钮模板）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/user-usergroup.html`
 *    （源 `.tools/recon/suqing1/html/User__usergroup.html` 的 **L52-86**）
 *  - 列定义与 `table.render` 参数 → `.tools/recon/suqing1/assets/JS2019/User/usergroup.js`
 *    **L19-42**：`elem:'#tableList'`、`height:'full'`（L21）、`defaultToolbar: []`（L23，
 *    **本页只写了一次**，不是「同对象写两次、后写生效」那一类）、`page: false`（L39）
 *  - 行内按钮模板 → 同上 HTML 的 `#baroperating`（**L82-84**）
 *
 * ⚠️ **本页的 `defaultToolbar` 是 `[]`**（其余同批 4 页都是 `['filter']`）：探针实测对标
 * `selfIcons = null`（表格右上角没有「筛选列」图标），漏掉这一条会多出图标并判 `struct`。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「角色职位分组」（`User__usergroup.html` **L5**）。
 *
 * ## 后端复核（2026-09-21，「尚未开放」逐个复核）
 *
 * **结论：本页继续保持「尚未开放」——后端没有这个模型、也没有这个端点。** 证据（两路独立）：
 *  1. **模型**：`apps/rbac/models.py` 里 `Role.group` 是与**同名 `auth.Group`** 的 `OneToOne`
 *     （角色 = 一个职位，`Role` 与 `Group` 事务同步），**没有**「一个分组包含多个角色职位」的表/关系；
 *     对标本页的 `containJobName`（「包含具体角色职位」是**多值**）在我们的数据里无对应物。
 *  2. **端点**：`grep -rn "group" src/apps/<app>/urls.py src/apps/core/urls.py src/config/urls.py`
 *     → **0 命中**；`grep -rn "router.register" src/apps/<app>/urls.py` 只有九个
 *     （`accounts` / `games` / `servers` / `categories` / `projects` / `orders` /
 *     `performances` / `role-authorities` / `permission-nodes`）；`apps/rbac/views.py`
 *     只有 `RoleAuthorityViewSet` 与 `PermissionNodeViewSet`（RBAC spec §7 的端点表
 *     也只有 `/api/role-authorities/` 与 `/api/permission-nodes/` 两条前缀）。
 *
 * 所以本页**不发任何请求**：`table` 恒空、行内那颗「修改包含职位」按 D-0025 §5 的口径提示
 * 「尚未开放」（由 `artifactPage` 的捕获期收口统一处理）。**不要**为了「能点」去造后端没有的请求
 * （impl-brief §0.3），也**不要**拿 `Role` 列表冒充「分组」——那不是同一个概念。
 * 对标侧本页有 6 行数据（`parity` 的 `data` 差异），我们侧是 0 行；这条在接线报告里如实登记。
 */
import { artifactPage } from '@/layui/artifactPage'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/user-usergroup.html'

/**
 * 表头（逐条照抄 `usergroup.js` **L24-38** 的 `cols[0]`，共 3 列）。
 *
 * - `align:'left'` 逐字抄（layui 的 `align` 默认就是 left，但这里是**对标显式写的**，留证据）；
 * - `sort:false` / `unresize:false` 是 layui 默认值，不写；
 * - 操作列的 `fixed:''`（= 不固定）是 layui 默认，不写；`toolbar:'#baroperating'` **必须写**
 *   （漏了这列会静默变空列，`parity` 对空文案的特殊列不报差异）；
 * - 「包含具体角色职位」**没有 `width`**（L32）⇒ 自动列，由 layui 均分剩余宽度。
 *   实测对标渲染 `101 / 1417 / 131`（总和 1649 = 表宽），我们按同样声明得到同样的分配。
 */
export const COLS: ArtifactCol[] = [
  { field: 'groupName', title: '角色分组名称', width: 100, align: 'left', unresize: true },
  { field: 'containJobName', title: '包含具体角色职位', align: 'left', unresize: true },
  { title: '操作', width: 130, toolbar: '#baroperating', unresize: true },
]

/** 表的声明（`elem` / `page` / `defaultToolbar` / `height` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  // 对标 L39 `page: false`（**显式**关闭分页，不是「没写 page」那一类）。
  page: false,
  // 对标 L23 `defaultToolbar: []` —— 本页没有「筛选列」图标。
  defaultToolbar: [],
  // 对标 L21 `height: 'full'`：layui 里 `'full'` 这个字符串既不匹配 `/^full-.+$/`、
  // 也不是函数（实测 vendored `public/layui/layui.js` offset 280395 起的分支），
  // 所以两侧都走 `parseFloat('full') → NaN` 的同一条路径（等于没有高度约束）。
  // **逐字保留**，不要「修好它」——我们要的是与对标**同一条**代码路径。
  height: 'full',
  cols: COLS,
}

const base = artifactPage({ url: ARTIFACT_URL, tables: [TABLE] })

export default {
  mount: (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`User__usergroup.html` L5）。
    document.title = '角色职位分组'
    return base.mount(root, ctx)
  },
} satisfies IslandPage
