/**
 * 【交易订单自动调价机制设置】页的 layui island 复刻（对标 `/System/AutomaticAdJustPriceSet`，
 * 对标 `<title>交易订单自动调价机制设置</title>`）。
 *
 * ## 证据出处
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>`、body 标记、工具栏/行操作/开关模板、隐藏的 `.modifyParamDiv` 表单 | `public/replica/system-automaticadjustpriceset.html` ← `.tools/recon/suqing1/html/System__AutomaticAdJustPriceSet.html` **L24-41 + L51-174**（逐字；只删了 `<script src>` 与 HTML 注释，PII 无） |
 * | 表头 `cols` | `.tools/recon/suqing1/assets/JS2019/System/AutomaticAdJustPriceSet.js` **L51-91** |
 * | `table.render` 参数 | 同上 **L45-92** |
 *
 * ## 渲染参数取值（逐条）
 *
 * - `defaultToolbar` 写了两次（L48 `[]` → L50 `['filter']`）⇒ **后者生效**。
 * - `page:false`（L92）；无 `height`、无 `cellMinWidth`（对标原文没有，别自己加）。
 * - 我们**恒空、不发请求**：对标 `url:'/SystemAjax/GetAutomaticAdJustPriceSetList'`。
 *
 * ## 三处照抄、不要「修正」的原文
 *
 * 1. **两个开关列的 `templet` 是对标自己交叉配的**：`id5`（循环调价）用 `#switchTpl2`，
 *    而 `#switchTpl2` 的 `checked` 条件是 `d.str3 == 'Y'`；`str3`（是否启用）用 `#switchTpl1`，
 *    它的条件是 `d.id5 == 1`（`AutomaticAdJustPriceSet.js` L85/L88 与页内模板 L55-60）。
 *    这是对标自己的写法，**照抄**，不要「顺手改成对应字段」。
 * 2. `#projectType` 上挂的是第三方多选插件的属性（`xm-select-radio`/`xm-select-create`/
 *    `xm-select-search`，HTML L102）。它所在的 `.modifyParamDiv` 初始 `display:none`，
 *    页面加载时看不见；我们**没有**把它渲染成 xm-select（本轮的 vendor 注入只给
 *    `system-user-list` 那几个筛选项用过），所以它保持一个普通 `<select>`——
 *    属性原样留在标记里，layui 会忽略它们。
 * 3. 工具栏两颗按钮的说明写在 `title=` 上（不是 `lay-tips=`），图标是 layui 自带字形
 *    （`&#xe669;` / `layui-icon-add-1`）——照抄，图标正常显示。
 *
 * 其余列的 `templet` 语义（空表下不渲染，仅备查）：`str2` → `res.str2 || '全部'`；
 * `id3` → `1` 时 `<span style='color:#16baaa'>百分比`（对标模板字符串本身就是**不平衡**的，
 * 照抄），否则「固定价」；`d3` → `1` 时 `{值}%` 否则 `{值}元`；`str10` 是文本；
 * 最后一列 `toolbar:'#baroperating'`（行内「修改 / 删除」）。
 */
import { artifactPage, type ArtifactCol } from '@/layui/artifactPage'
import type { IslandPage } from '@/layui/island'

const page = artifactPage({
  url: 'replica/system-automaticadjustpriceset.html',
  tables: [
    {
      elem: '#tableList',
      // 逐条照抄 `AutomaticAdJustPriceSet.js` L51-91。
      cols: [
        { field: 'str1', title: '游戏名称', width: 180, align: 'center' },
        { field: 'str2', title: '项目分类', width: 150, align: 'center' },
        { field: 'id3', title: '加价模式', width: 80, align: 'center' },
        { field: 'd3', title: '每次加价(元)', width: 120, align: 'center' },
        { field: 'd2', title: '最多加价(元)', width: 120, align: 'center' },
        { field: 'id4', title: '加价频率(分)', width: 120, align: 'center' },
        { field: 'd4', title: '亏损上限(元)', width: 120, align: 'center' },
        { field: 'str10', title: '执行时间', width: 120, align: 'center' },
        // L85/L88：两个开关列（页内模板 `#switchTpl2` / `#switchTpl1`，`lay-text="启用|禁用"`）。
        { field: 'id5', title: '循环调价', width: 100, align: 'center', switchText: '启用|禁用' },
        { field: 'str3', title: '是否启用', width: 100, align: 'center', switchText: '启用|禁用' },
        // L90：`fixed:''`（不固定）、宽 150、`toolbar:'#baroperating'`。
        { title: '操作', width: 150, align: 'center' },
      ] satisfies ArtifactCol[],
      toolbar: '#toolbarDemo',
      defaultToolbar: ['filter'],
      page: false,
    },
  ],
})

export default {
  mount: async (root, ctx) => {
    const unmount = await page.mount(root, ctx)
    document.title = '交易订单自动调价机制设置'
    return unmount
  },
} satisfies IslandPage
