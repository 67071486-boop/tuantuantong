/**
 * 【新增/编辑账号】页的 **layui 复刻页**（对标物 `/GameAccounts/addeditaccount`，**证据件渲染**）。
 *
 * 形状：**长表单页**（无表格、无工具栏、无页签）。17KB，是全组第二大的表单页。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/gameaccounts-addeditaccount.html`
 *    （源 `.tools/recon/suqing1/html/GameAccounts__addeditaccount.html` 的 L52-304）
 *  - 页面行为 → `.tools/recon/suqing1/assets/JS2019/GameAccounts/addeditaccount.js`
 *    （`#gameAccId` > 0 才是「编辑」态，会回填各字段；**我们不做回填**，
 *    故所有 `<input>` 的 `value` 一律留空——这也是 PII 口径要求的）
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「新增/编辑账号」（`GameAccounts__addeditaccount.html` L5）。
 *
 * ⚠️ 页内 4 个 `xm-select` 控件（`gameArea`/`gameServer`/`gameJob`/`gameGroup`，`useType` 共 5 个）
 * 我们对标是普通 `<select>`（见报告「已知可接受差异」）。
 *
 * ⚠️ 行为口径（D-0025 §5）：保存 / 选文件 / 开关一律提示「尚未开放」；**不置灰**。
 *
 * ## ⚠️ 5 个 `xm-select` 控件：用对标那份 vendor 库渲染（**不自造**）
 *
 * 「大区/平台」「服务器」「游戏职业」「阵营门派」「用途类型」在对标上带 `xm-select-radio`
 * 等属性，由对标自带的 **formSelects v4** 渲染（它会把 `<select>` 藏起来并在后面插
 * `.xm-select-parent`）。仓库里已有对标那份**逐字节相同**的
 * `public/vendor/formselects-v4.js`（sha256 与 `/layuiadmin/lib/formSelects-v4.js` 实测一致），
 * 共享入口是 `@/layui/vendor` 的 `initFormSelects()`（`pages/system-user-list.ts` 已在用）。
 * ⇒ 本页照该既有做法调一次，**不另写多选控件**。
 *
 * 首屏 4 个 `xm-select` 是空的（对标靠 AJAX 拉大区/服务器/职业/阵营，我们不发请求），
 * 与对标「AJAX 未返回时」的渲染一致；`.groupdiv`（阵营门派）在对标上默认 `display:none`。
 */
import { artifactPage } from '@/layui/artifactPage'
import { initFormSelects } from '@/layui/vendor'
import type { IslandPage } from '@/layui/island'

export default {
  mount: async (root, ctx) => {
    document.title = '新增/编辑账号'
    const unmount = await artifactPage({ url: 'replica/gameaccounts-addeditaccount.html' }).mount(
      root,
      ctx,
    )
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
