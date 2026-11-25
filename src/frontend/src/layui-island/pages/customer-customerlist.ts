/**
 * 【会员管理】页的 **layui 复刻页**（对标物 `/Customer/customerlist`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（11 个筛选项 + 工具栏 7 颗按钮 + 表格 + 一个 `display:none` 的充值弹层）。
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/customer-customerlist.html`
 *    （源 `.tools/recon/suqing1/html/Customer__customerlist.html` 的 L43-末尾，逐字；仅删非标记 `<script>`）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Customer/customerlist.js` **L98-215**
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L190-201）、行内按钮 `#table_tool`（L202-209）
 *  - `#total` 文案模板 → `customerlist.js` **L224-227**
 *
 * ## 关于「表格级 toolbar 悬空引用」的核查结论（读码 + 实测）
 *
 * `.tools/replica-draft/REPORT.md` 把本页列进「表格级 toolbar 悬空引用」的 6 页之一。**复核后不成立**：
 *  - `#tableList` 的 `toolbar: '#toolbarDemo'`，而 `#toolbarDemo` **就在** HTML L190 里；
 *    实测对标渲染结果 `.layui-btn-container .layui-btn` = **7 颗**（新增会员/刷新列表/重算累计实充/
 *    重算累计消费/导出/会员统计排行榜/删除会员），与本页 `table.render` 的工具栏一致。
 *  - 真正「悬空」的是**第二张表** `#tableChart`（`toolbar: '#toolbartableChart'`）：它的
 *    `<table id="tableChart">` 与 `#toolbartableChart` 模板都在 `customerlist.js` **L446-451 的
 *    弹层 HTML 字符串**里，由「会员统计排行榜」按钮点开时才插进 DOM。**首屏不存在这张表**，
 *    所以本页**只**渲染 `#tableList` 一张表，不补 `#tableChart`。
 *
 * ## 为什么这一页仍在页面内 `table.render`（共享渲染器的现存缺口）
 *
 * `ArtifactTable` 现已能表达 `page` / `defaultToolbar` / `pageLimits`，但 **`ArtifactCol` 仍缺 `type`**：
 * `buildCol` 只搬 `field/title/width/minWidth/hide/align/fixed/sort/unresize/switchText/badge`，所以对标
 * 第一列的 `{ type: 'checkbox', fixed: 'left', width: 50 }`（表头那个勾选框，`customerlist.js` **L99**）
 * 用 `tables` 表达不出来。少了它表头就没有勾选框，而探针的表头比对**只读文案与宽度** → **不会报**
 * （静默差异），所以不能靠「反正探针绿」蒙过去。
 * 因此本页只借 `artifactPage` 的证据件注入与「点了提示未开放」的收口，表格用 layui 原生
 * `table.render` 按对标 JS 逐项复刻，**一份列定义、不做二次覆盖**（不是 `reload` 修补）。
 * 缺口已报主会话：建议给 `ArtifactCol` 补 `type?: 'checkbox' | 'numbers' | 'radio'`。
 *
 * 分页取值（逐字取自页面 JS）：`page: true` / `limit: 20`（**L217**）/ `limits: [50,100,150,200]`（**L218**）。
 */
import { artifactPage } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'

/** 证据件（对标页 `<style>` 段 + body 标记，零转义，可直接与对标原文逐行对拍）。 */
const ARTIFACT_URL = 'replica/customer-customerlist.html'

const base = artifactPage({ url: ARTIFACT_URL })

/** 逐条照抄 `customerlist.js` L98-215（`sort:false`/`unresize:false` 是 layui 默认值，故不写）。 */
const COLS: readonly Record<string, unknown>[] = [
  { type: 'checkbox', fixed: 'left', width: 50 },
  // 下面 8 列的 `minWidth`（大写 W）在 `customerlist.js` 里是**真**属性，必须保留：
  // 它们是对标的「自动列」，实测渲染宽度由「表宽 − 定宽列之和」均分得出
  // （会员号 91 / 折扣模式 78 / 备注 74 …），换成无效的小写 `minwidth` 会撑破总宽。
  { field: 'customerNo', title: '会员号', minWidth: 90, align: 'center' },
  { field: 'bossName', title: '会员名称', minWidth: 90, align: 'center' },
  { field: 'levelName', title: '会员等级', minWidth: 80, align: 'center' },
  { field: 'phone', title: '电话', minWidth: 86, align: 'center' },
  { field: 'wxNo', title: '微信号', minWidth: 70, align: 'center' },
  { field: 'buyDiscount', title: '折扣', width: 60, align: 'center' },
  { field: '', title: '折扣模式', minWidth: 67, align: 'center' },
  { field: 'platName', title: '平台', width: 60, align: 'center' },
  { field: 'arrearsNote', title: '欠款备注', minWidth: 80 },
  { field: 'balance', title: '当前余额', minWidth: 60, align: 'center', sort: true },
  { field: 'totalConsAmount', title: '总计消费', minWidth: 60, align: 'center', sort: true },
  { field: 'historRecharge', title: '总计实充', minWidth: 60, align: 'center', sort: true },
  { field: 'levelGap', title: '距下次升级', width: 70, align: 'center' },
  { field: 'servicePerson', title: '服务人员', width: 60, align: 'center' },
  { field: 'createUser', title: '创建人员', minWidth: 60, align: 'center' },
  { field: 'tips', title: '附加信息1', minWidth: 60 },
  { field: 'remark', title: '备注', minWidth: 60 },
  { field: 'brithDay', title: '会员生日', width: 60, align: 'center' },
  { field: 'createTime', title: '创建时间', width: 76, align: 'center' },
  // `templet: '#table_tool'`（对标 L212）：空表下不渲染，模板本身**逐字**躺在证据件里。
  { field: 'createPersonUserNicke', title: '操作', width: 154, templet: '#table_tool' },
]

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 对标 `<title>` 是服务端渲染的（`Customer__customerlist.html` L5）。
    document.title = '会员管理'
    const unmount = await base.mount(root, ctx)
    const layui = await bootstrapIsland(['table'])
    const table = layui.table as unknown as { render: (o: unknown) => void }
    const form = layui.form as unknown as { render: () => void }
    table.render({
      elem: '#tableList',
      // 我们的后端没有这条接口 → **恒空、不发任何请求**（对标这里是 url + GET）。
      data: [],
      toolbar: '#toolbarDemo',
      defaultToolbar: ['filter'],
      cols: [COLS],
      page: true,
      limit: 20,
      limits: [50, 100, 150, 200],
      text: { none: '暂无数据' },
      // 对标 L224-227 的 `done` 文案模板，逐字搬；空表 → count/ext 全 0，渲染出与对标**同一条**统计文案
      // （对标该租户也是 0 个会员，实测 `#total` = 「本次搜索共计0个会员，累计可用余额0元，累计消费0元」）。
      done: (res: { count?: number; ext?: Record<string, number> }) => {
        const el = root.querySelector('#total')
        if (!el) return
        const ext = res.ext
        el.innerHTML =
          `本次搜索共计${res.count ?? 0}个会员，` +
          `累计可用余额${ext && ext.totalBalance ? ext.totalBalance : 0}元，` +
          `累计消费${ext && ext.totalConsumeFee ? ext.totalConsumeFee : 0}元`
        form.render()
      },
    })
    return unmount
  },
} satisfies IslandPage
