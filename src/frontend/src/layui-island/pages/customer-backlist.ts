/**
 * 【黑名单管理】页的 **layui 复刻页**（对标物 `/Customer/backlist`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（1 个搜索框 + 工具栏 3 颗按钮 + 表格；无弹层标记）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/customer-backlist.html`
 *    （源 `.tools/recon/suqing1/html/Customer__backlist.html` 的 L43-末尾，逐字；仅删非标记 `<script>`）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Customer/backlist.js` **L34-58**
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L54-62）
 *
 * ## 值纪律（两处容易抄错的）
 *  - 工具栏 3 颗按钮都是 `layui-btn-xs`（22px），**不是** `-sm`；图标类
 *    `layui-refresh`/`layui-heimingdan1`/`layui-shanchu` 是对标**私有字形**，我们保留 `<i>` 结构、
 *    字形不显示（见 `docs/FRONTEND-REPLICA-STANDARD.md` §7），**不要**为凑宽度改 padding/字号。
 *  - `原因备注` 列**没有** `width`（JS L46-51）→ 它是对标的自动列，实测渲染 1014px（吸走剩余宽度）。
 *    补 `width`/`minWidth` 会撑破总宽，务必保持不填。
 *  - `加入时间` 列的 `templet` 里读的是 `res.createTime`（JS L54）——对标自己的字段名不一致，登记不修。
 *
 * 为什么这一页仍用页面内 `table.render`：第一列是 `{ type: 'checkbox', fixed: 'left', width: 30 }`
 * （`backlist.js` **L35**），而 `ArtifactCol` 至今**没有 `type`** 字段 → 用 `tables` 会**静默**少掉
 * 表头勾选框（探针只读表头文案/宽度，不会报）。缺口与建议见 `pages/customer-customerlist.ts`
 * 顶部同段说明；本页只借 `artifactPage` 的证据件注入与点击收口。
 *
 * 分页取值（逐字取自页面 JS）：`page: true` / `limit: 50`（**L60**）/ `limits: [10,20,30,40,50,80,100]`（**L61**）。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'

/** 证据件（对标页 `<style>` 段 + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/customer-backlist.html'

const base = artifactPage({ url: ARTIFACT_URL })

/** 逐条照抄 `backlist.js` L34-58（`sort:false`/`unresize:false` 是 layui 默认值，故不写）。 */
const COLS: readonly Record<string, unknown>[] = [
  { type: 'checkbox', fixed: 'left', width: 30 },
  { field: 'moblie', title: '手机号', width: 150, fixed: 'left', align: 'left' },
  { field: 'ww', title: '旺旺或YY', width: 150, align: 'center' },
  { field: 'qq', title: 'QQ或微信', width: 150, align: 'center' },
  // 自动列（对标**没有** width）→ 实测渲染 1014px。**不要**补 width/minWidth。
  { field: 'backCount', title: '原因备注', align: 'left' },
  { field: 'lastTime', title: '加入时间', width: 150, align: 'center' },
]

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Customer__backlist.html` L5）。
    document.title = '黑名单管理'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['table'])
    const table = layui.table as unknown as { render: (o: unknown) => void }
    table.render({
      elem: '#tableList',
      // 我们的后端没有这条接口 → **恒空、不发任何请求**。
      data: [],
      toolbar: '#toolbarDemo',
      defaultToolbar: ['filter'],
      cols: [COLS],
      // 对标 L31/L33 同一个对象里写了两次 `defaultToolbar`，**后者生效** → `['filter']`（上面已按生效值）。
      page: true,
      limit: 50,
      limits: [10, 20, 30, 40, 50, 80, 100],
      text: { none: '暂无数据' },
    })
    return unmount
  },
} satisfies IslandPage
