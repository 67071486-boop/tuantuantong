/**
 * 【导入账号】页的 **layui 复刻页**（对标物 `/GameAccounts/importacc`，**证据件渲染**）。
 *
 * 形状：**纯表单页**（无表格、无工具栏）。筛选/表单区是一个 `.layui-form-pane`，
 * 三组单选 + 一个「选择导入文件」按钮。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/gameaccounts-importacc.html`
 *    （源 `.tools/recon/suqing1/html/GameAccounts__importacc.html` 的 L52-135；
 *    该页**没有**页内 `<style>`——它唯一的 `<style>` 是全站 layout 的 `.layadmin-pagetabs`，
 *    已按「只取页面自己的值」的口径剔除）
 *  - 页面行为 → `.tools/recon/suqing1/assets/JS2019/GameAccounts/importacc.js`
 *    （`upload.render` 选文件后才切 `.ge1/.ge2/.ge3/.ge4` 模板下载项，**首屏只显示 `.ge1`**）
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「导入账号」（`GameAccounts__importacc.html` L5）。
 *
 * ⚠️ 行为口径（D-0025 §5）：文件选择按钮点了提示「尚未开放」（`artifactPage` 内建），
 * **不置灰**、不隐藏；不发任何写请求。
 */
import { artifactPage } from '@/layui/artifactPage'
import type { IslandPage } from '@/layui/island'

export default {
  mount: async (root, ctx) => {
    document.title = '导入账号'
    return artifactPage({ url: 'replica/gameaccounts-importacc.html' }).mount(root, ctx)
  },
} satisfies IslandPage
