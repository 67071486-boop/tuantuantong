/**
 * 【售卖账号预览】页的 **layui 复刻页**（对标物 `/GameAccounts/sellaccpreview`，**证据件渲染**）。
 *
 * 形状：**筛选栏 + 卡片看板**（无表格、无工具栏）。这是全组唯一一个「表在 JS 里现拼」的页面。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/gameaccounts-sellaccpreview.html`
 *    （源 `.tools/recon/suqing1/html/GameAccounts__sellaccpreview.html` 的 L52-189）
 *  - 页面行为 → `.tools/recon/suqing1/assets/JS2019/GameAccounts/sellaccpreview.js`
 *
 * ## 为什么这里**没有**表格、也没有工具栏（三处「对标悬空引用」的处置）
 *
 * 草稿提示 `toolbar:'#toolbarDemo'` 在 HTML 里没有对应模板。回渲染后的对标页核实：
 * `gettabledata()` 是 AJAX 拉数据后**逐组现拼** `<div class="tableitem layui-col-md4">` +
 * `<table class="layui-hide" id="tableList0">`，再对每个 `#tableListN` 调 `table.render`
 * （`sellaccpreview.js` 的 `success` 回调），其 `toolbar` 是**同一个回调里现写的**
 * `'#total'+index`（`<div class="layui-hide" id="total0">`）。所以：
 *  1. `#tableList`（无下标）**只出现在 JS 的失败分支所需的位置**、静态 HTML 里没有 →
 *     页面报「JS 里有 1 个 table.render 的 elem 在 HTML 里找不到」；
 *  2. `toolbar:'#total'`（无下标）是**分析器把模板字符串 `'#total'+index` 截断**得到的假引用；
 *     对标自己也不存在 `#total` 这个 id，真正的 id 是 `#total0`/`#total1`…；
 *  3. `data.code <= 0`（我们恒无后端）时对标执行的是 `$('.tables').append('<div class="nodata">暂无数据</div>')`
 *     ——**实测对标页就是这个结果**（playwright 打开对标页，`.tables` 里只有 `.nodata`，
 *     `.layui-table-view` 数量为 0，工具栏按钮数 0）。
 *
 * ⇒ 复刻「对标页在无数据时的实际渲染」= 只有筛选栏 + 空的 `.tables` + `.nodata`。
 * **不补** `#toolbarDemo` 模板、**不**凭空造一个表格（那会造出对标没有的 `.layui-table-view`）。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「售卖账号预览」（`GameAccounts__sellaccpreview.html` L5）。
 *
 * ## ⚠️ 2 个 `xm-select` 控件：用对标那份 vendor 库渲染（**不自造**）
 *
 * 「大区/平台」「服务器」在对标上带 `xm-select-radio xm-select-create …`，是对标自带的
 * **formSelects v4** 渲染的：它会把 `<select>` 藏起来（加 `lay-ignore`）并在后面插
 * `.xm-select-parent`（实测这些 `<select>` 在对标页 `offsetParent === null`）。
 * 仓库里已有对标那份**逐字节相同**的 `public/vendor/formselects-v4.js`
 * （sha256 与 `/layuiadmin/lib/formSelects-v4.js` 实测一致），共享入口是
 * `@/layui/vendor` 的 `initFormSelects()`（`pages/system-user-list.ts` 已在用）。
 * ⇒ 本页照该既有做法调一次，**不另写多选控件**。
 *
 * ⚠️ 行为口径（D-0025 §5）：搜索 / 排序 / 选大区一律提示「尚未开放」；**不置灰**。
 */
import { artifactPage } from '@/layui/artifactPage'
import { initFormSelects } from '@/layui/vendor'
import type { IslandPage } from '@/layui/island'

export default {
  mount: async (root, ctx) => {
    document.title = '售卖账号预览'
    // `tables` 故意留空：对标页无数据时没有表格（见上方注释的实测依据）。
    const unmount = await artifactPage({ url: 'replica/gameaccounts-sellaccpreview.html' }).mount(
      root,
      ctx,
    )
    // 对标 `sellaccpreview.js` 的 `getGameArea()` 之后由 formSelects 渲染（它自己在构造时也会扫一遍）。
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
