/**
 * 【指定给我的账号】页的 **layui 复刻页**（对标物 `/GameAccounts/myaccountlist`，**证据件渲染**）。
 *
 * 形状：**单表列表页**（筛选栏 10 个 label / 8 个下拉 + 2 个文本框 + 搜索按钮
 * + 工具栏 3 颗按钮 + 表格 + 1 个 `display:none` 的进度状态弹层）。
 *
 * 证据：
 *  - 标记与页内 `<style>` → `public/replica/gameaccounts-myaccountlist.html`
 *    （源 `.tools/recon/suqing1/html/GameAccounts__myaccountlist.html` 的 L52-250）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/GameAccounts/myaccountlist.js` 的 `table.render`
 *    （`elem:'#tableList'`、`toolbar:'#toolbarDemo'`、
 *    `defaultToolbar:[]` 后又写 `defaultToolbar:['filter']`（**后者生效**）、
 *    `page:true`、`limit:20`、`limits:[10,20,30,40,50]`）
 *  - 工具栏模板 → 同上 HTML 的 `#toolbarDemo`（L214-221）；隐藏弹层 → `.setAccountProStatus`（L224-248）
 *
 * ## 表格用 `artifactPage` 的声明式 `tables`（共享渲染器已支持本页需要的字段）
 *
 * 本页原先因「勾选列 + `defaultToolbar`」而自渲染；共享渲染器补齐
 * `ArtifactTable.page` / `defaultToolbar` 之后，**改回声明式**（不再 `table.reload`）。
 * 唯一仍表达不了的是**勾选列**（`ArtifactCol` 没有 `type`，而 `buildCol` 是白名单复制——
 * 未知键会被丢掉）⇒ 见 `COLS` 第一项的断言与注释。
 *
 * ## ⚠️ 15 个百分比列宽的处置（**人工定值，来自实测**）
 *
 * 对标 16 个数据列里有 **15 列**的 `width` 是运行期表达式 `isPc ? '<N>%' : <px>`
 * （只有「账号截图」是字面量 `80`）。按 `impl-brief.md` §「已知的可接受差异」取**桌面分支**
 * （`isPc = true`，`myaccountlist.js` L10-14：只有 android/ios 才置 false）。
 *
 * layui 2.13 的 `setColsWidth()` 确实支持 `'<N>%'` 声明值（正则 `/\d+%$/`），会在渲染时
 * 按**当时的表宽**换算成像素；我们这个模块是**静态声明**，给不出「运行期表宽」，
 * 所以按 **1650 视口下对标的实测渲染宽度**定值（`.tools/probe-layout` / playwright 实测），
 * 并保留对标的 authored 百分比在注释里备查：
 *
 * | 列 | 对标 authored | 实测宽(1650) |
 * |---|---|---|
 * | 账号类型 | `5%` | 83 |
 * | 角色名 | `7%` | 115 |
 * | 阵营 | `4%` | 66 |
 * | 角色信息 | `13%` | 213 |
 * | 游戏账号 | `9%` | 148 |
 * | 子账号 | `4%` | 66 |
 * | 账号状态 | `5%` | 83 |
 * | 解封时间 | `5%` | 83 |
 * | 用途类型 | `7%` | 115 |
 * | 进度状态 | `4%` | 66 |
 * | 最后排团时间 | `6%` | 99 |
 * | 接手人 | `5%` | 83 |
 * | 入库时间 | `5%` | 83 |
 * | 账号截图 | `80`（字面量） | 81 |
 * | 备注 | `7%` | 115 |
 * | 机位 | `7%` | 115 |
 *
 * ⚠️ 右列实测值**只是第一版的人工换算**，已被下面这条推翻：
 *
 * **做法：把对标的 `'<N>%'` 原样透传，不换算成像素。** layui 2.13 的 `setColsWidth()`
 * **原生支持 `'<N>%'` 声明值**（正则 `/\d+%$/`，按表宽换算）。我第一版按 1650 视口把百分比
 * 换算成固定像素（83/115/66/…），探针在 **x 坐标**上仍逐列差 1–4px
 * （对标 230/296/509 vs 我们 232/299/513，向后累加漂移）——因为
 * `Math.round(parseFloat(w)/100*u)` 与我们手抄的整数并不总相等。
 * 透传后由**同一份 layui 算法**算**同一份输入**，视口变了也跟对标一起缩放，**没有人工地板**。
 *
 * ⚠️ 对标 `<title>` 是服务端渲染的「指定给我的账号」（`GameAccounts__myaccountlist.html` L5）。
 *
 * ## ⚠️ 5 个 `xm-select` 控件：用对标那份 vendor 库渲染（**不自造**）
 *
 * 「账号类型」「大区/平台」「服务器」「阵营门派」「用途类型」带 `xm-select-radio` 等属性，
 * 由对标自带的 **formSelects v4** 渲染（它会把 `<select>` 藏起来并在后面插 `.xm-select-parent`）。
 * 仓库里已有对标那份**逐字节相同**的 `public/vendor/formselects-v4.js`
 * （sha256 与 `/layuiadmin/lib/formSelects-v4.js` 实测一致），共享入口是
 * `@/layui/vendor` 的 `initFormSelects()`（`pages/system-user-list.ts` 已在用）。
 * ⇒ 本页照该既有做法调一次，**不另写多选控件**。
 */
import { artifactPage } from '@/layui/artifactPage'
import type { ArtifactCol, ArtifactTable } from '@/layui/artifactPage'
import { initFormSelects } from '@/layui/vendor'
import type { IslandPage } from '@/layui/island'

/** 证据件（页内 `<style>` + body 标记，零转义）。 */
const ARTIFACT_URL = 'replica/gameaccounts-myaccountlist.html'
/** 对标 `page.limits`（`myaccountlist.js`：`limits: [10, 20, 30, 40, 50]`）。 */
export const PAGE_LIMITS = [10, 20, 30, 40, 50]

/**
 * 勾选列（`myaccountlist.js` L106：`{ type:'checkbox', fixed:'left', width:30 }`）。
 * 共享渲染器（`ArtifactCol.type`）已支持——**不要**断言绕过：缺 `type` 该列会静默变成
 * 普通列（表头勾选框消失、列宽被撑开），而探针的表头比对只读文案+宽度，不会报。
 */
const CHECKBOX_COL: ArtifactCol = { type: 'checkbox', fixed: 'left', width: 30 }

/**
 * 百分比列宽**原样透传**（不换算成像素）。
 *
 * layui 的 `setColsWidth()` 认 `/\d+%$/` 的 `width` 并按当时的表宽换算；
 * 把对标的 `isPc ? '<N>%' : <px>` 取桌面分支后原样给它，才能与对标用**同一份算法**
 * 算**同一份输入**（第一版换算成整数像素时 x 坐标逐列漂 1–4px，见文件头注释）。
 *
 * `ArtifactCol.width` 的类型是 `number`，装不下 `'5%'` 这种字符串；这里用 `unknown` 过桥，
 * 并在注释里写明该列的真实声明值（layui 侧照样按字符串处理）。
 */
const PC = (v: string): number => v as unknown as number

/**
 * 表头（逐条照抄 `myaccountlist.js` 的 `cols[0]`，共 17 列含勾选列）。
 *
 * 列宽写的是对标的**桌面分支**原文（`PC('5%')` = 对标 `width: isPc ? '5%' : 70`）；
 * `unresize:false` / `sort:false` 是 layui 默认值，不写。
 */
export const COLS: ArtifactCol[] = [
  CHECKBOX_COL,
  // 对标 `width: isPc ? '5%' : 70`
  { field: 'accType', title: '账号类型', align: 'center', width: PC('5%') },
  // 对标 `width: isPc ? '7%' : 120`
  { field: 'gameRoleName', title: '角色名', width: PC('7%') },
  // 对标 `width: isPc ? '4%' : 70`，`unresize:true`
  { field: 'gameGroup', title: '阵营', align: 'center', width: PC('4%'), unresize: true },
  // 对标 `width: isPc ? '13%' : 160`
  { field: 'gameInfo', title: '角色信息', width: PC('13%') },
  // 对标 `width: isPc ? '9%' : 120`
  { field: 'gameAcc', title: '游戏账号', align: 'center', width: PC('9%') },
  // 对标 `width: isPc ? '4%' : 60`
  { field: 'zgameAcc', title: '子账号', align: 'center', width: PC('4%') },
  // 对标 `width: isPc ? '5%' : 70`
  { field: 'accStatus', title: '账号状态', align: 'center', width: PC('5%') },
  // 对标 `width: isPc ? '5%' : 80`，`sort:true`
  { field: 'tempDisableTime', title: '解封时间', align: 'center', width: PC('5%'), sort: true },
  // 对标 `width: isPc ? '7%' : 90`
  { field: 'accUseType', title: '用途类型', align: 'center', width: PC('7%') },
  // 对标 `width: isPc ? '4%' : 60`
  { field: 'proStatus', title: '进度状态', align: 'center', width: PC('4%') },
  // 对标 `width: isPc ? '6%' : 100`，`sort:true`
  { field: 'orderSoure', title: '最后排团时间', align: 'center', width: PC('6%'), sort: true },
  // 对标 `width: isPc ? '5%' : 80`
  { field: 'recUserName', title: '接手人', align: 'center', width: PC('5%') },
  // 对标 `width: isPc ? '5%' : 80`，`sort:true`
  { field: 'createTime', title: '入库时间', align: 'center', width: PC('5%'), sort: true },
  // 对标唯一的字面量列宽 `width: 80`
  { field: 'img', title: '账号截图', align: 'center', width: 80 },
  // 对标 `width: isPc ? '7%' : 100`
  { field: 'remark', title: '备注', width: PC('7%') },
  // 对标 `width: isPc ? '7%' : 70`
  { field: 'jiwei', title: '机位', width: PC('7%') },
]

/** 表的声明（`elem` / `toolbar` / `page` / `defaultToolbar` 与对标同值）。 */
export const TABLE: ArtifactTable = {
  elem: '#tableList',
  toolbar: '#toolbarDemo',
  // 对标同对象里 `defaultToolbar` 写了两次，第二次 `['filter']` 生效。
  defaultToolbar: ['filter'],
  page: { limit: 20, limits: PAGE_LIMITS },
  cols: COLS,
}

export default {
  mount: async (root, ctx) => {
    // 对标 `<title>` 是服务端渲染的（`GameAccounts__myaccountlist.html` L5）。
    document.title = '指定给我的账号'
    const unmount = await artifactPage({ url: ARTIFACT_URL, tables: [TABLE] }).mount(root, ctx)
    // 对标 `myaccountlist.js` 的 formSelects 渲染（5 个 `xm-select`，见文件头注释）。
    initFormSelects('select[xm-select]')
    return unmount
  },
} satisfies IslandPage
