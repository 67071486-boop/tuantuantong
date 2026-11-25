/**
 * 【系统用户实名认证记录与配置】页的 **layui 复刻页 + 接真后端**
 * （对标物 `/User/RnameCertLogAndSetting`）。
 *
 * 形状：**单表列表页 + 一个 `display:none` 的「实名认证相关设置」弹层**
 * （筛选栏 8 个控件 + 搜索按钮 + 工具栏 3 颗按钮 + 表格，**没有行内操作列**）。
 *
 * ## 证据（全部离线，**视觉值唯一出处**）
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>`（两段）、筛选栏、工具栏模板、`display:none` 的配置弹层、`<title>` | `public/replica/user-rnamecertlogandsetting.html` ← `.tools/recon/suqing1/html/User__RnameCertLogAndSetting.html`（**本轮一字未动**） |
 * | 列定义（10 个标题列 + 1 勾选列）与四个 `templet` | `.tools/recon/suqing1/assets/JS2019/User/RnameCertLogAndSetting.js` **L40-100** |
 * | `table.render` 参数 | 同上 **L33-108**：`elem:'#tableList'`、`toolbar:'#toolbarDemo'`（L37）、`defaultToolbar: []`（L36）后又写 `defaultToolbar: ['filter']`（L38，**后者生效**）、`page:true`（L102）/ `limit:20`（L103）/ `limits:[20]`（L104，**只有一档**） |
 * | **两段式取数** | 同上：**L27,35**（首屏 `url` 上拼的 `seachParam` 只有 `sdate`/`edate`，`table.render` 发的就是它）vs **L147-154**（`reloadOrderList` 里**每次都**从 DOM 现读并拼上 `status`/`key1`/`key2`/`key3`）——⚠️ 「刷新」（**:115-117**）与「搜索」（**:146**）走的是**同一个** `reloadOrderList` ⇒ **两边都带全筛选**（⛔ **不是**「点过搜索才带筛选」，那个开关是错的口径，见 `reload` 的长注释） |
 * | 认证图片的法律提示原文与按钮 | 同上 **L165-189**（`layer.confirm` 的文案与两个按钮逐字） |
 * | 配置弹窗的 `area` / `skin` / `anim` / `shade` | 同上 **L116-125** |
 * | 配置弹层 4 项的**问句与选项原文** | 证据件 `.settingDiv`（本页**直接渲染它**，一个字都没重写） |
 *
 * ## ⛔ 为什么不再用 `artifactPage`（与 `order-tradingorderactionloglist.ts` 同一条理由）
 *
 * `artifactPage.mountArtifact` 在**捕获阶段**把所有 `button / a.layui-btn / [lay-event] / [lay-filter]`
 * 的点击收口成「尚未开放」并 `stopPropagation`（`artifactPage.ts:282-298`）。而本页工具栏三颗按钮
 * **就是** `lay-event="refresh" / "settingBtn" / "buyBtn"`（证据件 L136-138）⇒ 挂着 `artifactPage`
 * 时**永远点不到** `table.on('toolbar(tableList)')`。所以本页照 `system-systemlog.ts` /
 * `order-tradingorderactionloglist.ts` 的形态改成**自挂载**：取证据件 → 注入 → `initIslandElements`
 * → 自己 `table.render`。
 * 代价：artifactPage 的「尚未开放」兜底提示也没有了 ⇒ 本页自己把「认证图片」那一列的行为显式接管
 * （见 `onCertImageClick`），其余可点元素（页签之类）本页没有。
 * 依据：`AGENTS.md` §5-G「自建 island 页必须有一条驱动真交互的页测试」。
 *
 * ## 与对标的**有意偏离**（每条都有依据，⛔ 不许照对标「修」回去）
 *
 * | # | 偏离 | 依据 |
 * |---|---|---|
 * | 1 | 端点换成本仓库的 `GET /api/accounts/realname-logs/`；对标的 `/UserAjax/GetUserRnameCertLogList` 不再出现在代码里 | spec §9.1 |
 * | 2 | **两个日期框的默认值 = 近 30 天**（`today-29d` ~ `today`，运行期算），**⛔ 不照抄**证据件里那对会过期的死值 `2026-08-20` / `2026-09-20` | 业务方拍板（spec §14-P7 / §15）；已登记进 `docs/FRONTEND-REPLICA-STANDARD.md` |
 * | 3 | **购买按钮升级成「配置实名资源包」应用内配置**（T3，2026-10-16，业务方拍板；spec §2.4）：按钮文案 `配置实名资源包(当前余量N次)`（余量取值链不动，`buyButtonLabel`），点击**打开配置弹窗**（现有包列表掩码 + 「添加密钥包」表单；secret 框 `type=password`、提交后清空、任何响应不回显；成功后刷新列表）。⚠️ **改准史**：2026-10-10 拍板过「只弹一句『请联系客服购买』」（选项 B，spec §13-1）——那条 toast 路径与 `BUY_CONTACT_TEXT` 常量已于本线**删除**；对标是跳站外 `jidanpu.com/buy/…`，站外那条路本线也不做 | 业务方拍板（spec §2.4）；已登记 `docs/FRONTEND-REPLICA-STANDARD.md`（2026-10-10 B 那条已追加更正） |
 * | 4 | **配置弹层底部多一行两态提示**「「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用」（2026-10-10 增设时原文案是「本版本仅保存设置，接单 / 结算的相关限制尚未启用」；2026-10-16「未实名不允许接单」实装后按实改准，D-0245） | 业务方拍板增设（spec §14-P11 / §15）、2026-10-16 拍板只实装接单侧（spec `2026-10-16-realname-claim-gate-design.md` §2.4）；⛔ 不改就是**反向静默说谎**（接单侧已生效却说没启用）。已登记 |
 * | 5 | 配置弹层的 4 项走 `GET` 回填真值 + `PATCH` 提交（对标是 `POST /UserAjax/SaveCertSetting` 把整个表单 `JSON.stringify` 成 `param`） | spec §9.1：我们的端点读 / 写分开（`GET` / `PATCH`），且 `PATCH` 是**部分更新** |
 * | 6 | 「认证图片」列的值**本批恒空**（我们只有二要素、没有图）⇒ 列照渲染、点了提示「暂无认证图片」，⛔ **不置灰** | spec §13-2-4 + `AGENTS.md` §5-D |
 *
 * ⚠️ **对标的已知缺陷，不照抄**（`AGENTS.md` §5-D「不复刻对标缺陷」同族）：
 * `RnameCertLogAndSetting.js` 的 **L190-216**（`form.on('submit(saveSettingBtn)')` 整段）**被写在
 * `$(document).on('click', '.viewCertImg', …)` 的回调里面**（L165 开、L189 才 `});` 收口）⇒
 * 它在对标那边**只在用户点过一次「点击查看」之后才被注册**（第一次打开页面直接点「保存设置」
 * → 那个 `form.on` 还没绑上 → **保存静默无效**）。本页把「保存设置」绑在**挂载时**，
 * 这不是偏离对标的设计意图，而是**不复制它的作用域事故**。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G 之一，逐条照做）
 *
 * ① **类名与选择器分成两个常量**（`TABLE_ELEM` / `TABLE_ID`、`SETTINGS_PANEL_ID` /
 *    `SETTINGS_PANEL_SELECTOR`）——`class="${PANEL_SELECTOR}"` 会造出 `class=".x"`、永远匹配不到；
 * ② **以 `(` 开头的语句一律前置分号**（`;(table.on as …)(…)`；ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ **先 `await bootstrapIsland(...)` 挂样式、再塞页面标记**（`layui.css` 是运行期插 `<link>`，
 *    顺序反了会 FOUC），而 `initIslandElements(layui)` 相反**必须**留在塞标记**之后**。
 */
import {
  buildRealNameLogParams,
  buyButtonLabel,
  createRealNamePackage,
  getRealNameQuota,
  getRealNameSetting,
  listRealNamePackages,
  updateRealNameSetting,
  type RealNameCertMethod,
  type RealNameLogParams,
  type RealNameLogRow,
  type RealNamePackageRow,
  type RealNameSetting,
  type RealNameSettingPatch,
} from '@/api/realname'
import { loadConfigArtifact } from '@/layui-island/config-replica'
import { dayRangeDaysAgo, type DateRange } from '@/layui-island/dateRange'
// ⚠️ HTML 转义的唯一出处（`escape.ts` 自己的口径）：本页资源包弹窗把服务端来的文本拼进
//    `innerHTML` / `layer.msg` ⇒ 用户可控串（备注 / 密钥 Id / 报错回显）必须在这里转义。
import { escapeHtml } from '@/layui-island/escape'
import { formatCreatedAt } from '@/api/auditLogs'
import {
  bootstrapIsland,
  initIslandElements,
  type IslandContext,
  type IslandPage,
} from '@/layui/island'
import type { LayuiModule } from '@/layui/loader'

/* ────────────────────────────── 选择器/常量（陷阱 ①：**一律两个常量**） ────────────────────────────── */

/** 证据件名（= `public/replica/<key>.html`）。 */
export const ARTIFACT_KEY = 'user-rnamecertlogandsetting'

/** 表格 `elem`（对标 JS L34；HTML 的 `<table … id="tableList">`）。 */
export const TABLE_ELEM = '#tableList'
/**
 * 表格实例 id（= `lay-filter`；`table.reload` 的第一个实参）。
 * ⚠️ 传 `'#tableList'` 是**静默 no-op** ⇒ 与 `TABLE_ELEM` 分成两个常量（陷阱 ①）。
 */
export const TABLE_ID = 'tableList'
/** 表格 `lay-filter`（`table.on('toolbar(tableList)')` 的事件名就是它）。 */
export const TABLE_FILTER = 'tableList'
/** 工具栏模板选择器（对标 JS L37）。模板本体在证据件的 `<script type="text/html" id="toolbarDemo">` 里。 */
export const TOOLBAR_ELEM = '#toolbarDemo'
/** 生效的 `defaultToolbar`（对标 JS L36 `[]` → **L38 `['filter']` 生效**）。 */
export const DEFAULT_TOOLBAR = ['filter'] as const
/** 对标 `page.limit` / `page.limits`（JS L103-104：**只有一档**）。 */
export const PAGE_LIMIT = 20
export const PAGE_LIMITS = [20] as const

/** 配置弹层容器的 **id**（证据件里那个 `<div class="settingDiv" style="display:none">`）。 */
export const SETTINGS_PANEL_CLASS = 'settingDiv'
/** 同一个容器的 CSS 选择器（陷阱 ①：与上面的类名分成两个常量）。 */
export const SETTINGS_PANEL_SELECTOR = `.${SETTINGS_PANEL_CLASS}`
/** 「点击查看」（认证图片列）的类名（对标 JS L97 `class="viewCertImg"`）。 */
export const CERT_IMAGE_CLASS = 'viewCertImg'
/** 同一个锚的 CSS 选择器（陷阱 ①）。 */
export const CERT_IMAGE_SELECTOR = `.${CERT_IMAGE_CLASS}`

/**
 * 认证图片的**法律提示原文**（逐字照抄对标 `RnameCertLogAndSetting.js` **L168**）。
 * ⛔ 一个字都不许改：这是会给用户看的法律声明。
 */
export const CERT_IMAGE_NOTICE =
  '郑重提示：根据相关法律法规，查看的认证图仅用于核实当事人真实性，切不可随意将图片转发、传播、滥用，甚至用于人身攻击、侮辱、诋毁以及非法使用，否则以此引发的所有后果由您负责！'
/** 法律提示的两个按钮（逐字照抄同 JS L169 的 `btn: [...]`）。 */
export const CERT_IMAGE_NOTICE_BUTTONS = ['我已知晓，保证不滥用', '关闭'] as const
/**
 * 图片空值时的提示（**我方自拟**）。
 * ⚠️ 依据：`cert_image` 本批恒空（spec §13-2-4），而对标那一支是直接 `layer.photos` 一张空图
 * ⇒ 不说一句的话用户只会看到「点了没反应」（`AGENTS.md` §5-D 明文禁止）。
 */
export const CERT_IMAGE_EMPTY_TEXT = '暂无认证图片'

/**
 * 配置弹层底部那行**两态提示**（业务方拍板增设；2026-10-16 按实装进度改准文案，
 * D-0245 / spec `docs/specs/2026-10-16-realname-claim-gate-design.md` §2.4）。
 * ⚠️ 对标**没有**这一行 ⇒ 有意偏离，已登记。⛔ 不许删、也不许说错状态：
 * 「未实名不允许接单」已由接单 / 指派闸消费（接单侧 `claim.py::check_grab_limits`、
 * 指派侧 `order_writes.assign_order`），而「结算要求实名」**仍只存不拦**——
 * 删了或写旧文案都是**静默说谎**。
 */
export const SETTINGS_NOT_APPLIED_NOTE =
  '「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用'

/**
 * 余量**读到了 0** 时的提示（**我方自拟**；2026-10-10 拍板「只弹一句」时定稿，
 * 2026-10-16 购买按钮升级成**应用内配置**（spec §2.4）后把「请点击购买按钮」那半句改准
 * ——按钮不再是「购买」，指路也跟着指到配置弹窗）。
 *
 * ⚠️ **为什么必须有这一句**：`0` 与 `null` 是**两种不同的状态**（`AGENTS.md` §5-E / `E-0021`）
 * ——`0` 是**合法读数**（买了用完了），`null` 是**读不到**（跨 schema 读失败，见 `api/realname.ts`
 * 的 `buyButtonLabel`）。而那颗按钮的**文案**两档**必须**都回落到字面量
 * `(当前余量0次)`（半角括号沿用证据件形状）⇒ 两档在按钮上看**一模一样**。
 * 若不额外说一句，租户就分不清「我的包用完了」与「页面没读到」，
 * 而后者正是 `AGENTS.md` §5-E 要避免的「把读不到当成读数」。
 */
export const QUOTA_EXHAUSTED_TEXT =
  '本租户实名认证余量已用完（当前余量 0 次），可在「配置实名资源包」中添加新密钥'

/* ────────────────────────── 配置实名资源包弹窗（T3，spec §2.4） ────────────────────────── */

/** 配置弹窗容器的类名（拼标记用）与选择器（查节点用）——陷阱 ①：两个常量。 */
export const PACKAGES_PANEL_CLASS = 'realnamePkgDiv'
export const PACKAGES_PANEL_SELECTOR = `.${PACKAGES_PANEL_CLASS}`
/** 「添加密钥包」按钮与各输入框的 `name`（自建标记，本页自己的命名空间）。 */
export const PKG_SUBMIT_ATTR = 'data-pkg-submit'
export const PKG_INPUT_NAMES = {
  keyId: 'pkgKeyId',
  keySecret: 'pkgKeySecret',
  totalQuota: 'pkgTotalQuota',
  expiresAt: 'pkgExpiresAt',
  remark: 'pkgRemark',
} as const
/** 弹窗里列表行的行类名（刷新重铺用）。 */
export const PKG_ROW_CLASS = 'realnamePkgRow'

/** 添加成功后的提示（**我方自拟**；弹窗里列表会自己刷新，这一句只说结果）。 */
export const PKG_CREATED_TEXT = '密钥包已添加'
/** 列表读取失败的兜底文案（后端文案通常已经有了）。 */
export const PKG_LIST_FAILED_TEXT = '密钥包列表读取失败'

/**
 * 弹窗标记（**自建**，⛔ 不动证据件：证据件是对标渲染结果的留档）。两块：
 * ① 现有包列表（掩码 key_id / 余量 / 状态 / 有效期 / 备注）；
 * ② 「添加密钥包」表单——⛔ secret 框 `type="password"`，提交后清空，任何响应不回显。
 */
export function buildPackagesPanelMarkup(): string {
  return `
<div class="${PACKAGES_PANEL_CLASS}" style="display:none;padding:15px 20px;">
  <div style="font-size:14px;font-weight:700;margin:0 0 8px">现有密钥包（密钥只显示前 4 位）</div>
  <table class="layui-table" style="margin:0 0 15px">
    <thead><tr><th>密钥 Id</th><th>余量</th><th>状态</th><th>有效期至</th><th>备注</th></tr></thead>
    <tbody data-pkg-rows></tbody>
  </table>
  <div style="font-size:14px;font-weight:700;margin:0 0 8px">添加密钥包</div>
  <div class="layui-form-item">
    <label class="layui-form-label">密钥 Id</label>
    <div class="layui-input-block"><input type="text" name="${PKG_INPUT_NAMES.keyId}" autocomplete="off" class="layui-input"></div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">密钥 Key</label>
    <div class="layui-input-block"><input type="password" name="${PKG_INPUT_NAMES.keySecret}" autocomplete="new-password" class="layui-input"></div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">总次数</label>
    <div class="layui-input-block"><input type="number" name="${PKG_INPUT_NAMES.totalQuota}" min="1" step="1" autocomplete="off" class="layui-input"></div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">有效期至</label>
    <div class="layui-input-block"><input type="text" name="${PKG_INPUT_NAMES.expiresAt}" placeholder="如 2027-10-10，留空 = 不过期" autocomplete="off" class="layui-input"></div>
  </div>
  <div class="layui-form-item">
    <label class="layui-form-label">备注</label>
    <div class="layui-input-block"><input type="text" name="${PKG_INPUT_NAMES.remark}" placeholder="选填" autocomplete="off" class="layui-input"></div>
  </div>
  <button type="button" class="layui-btn layui-btn-normal" ${PKG_SUBMIT_ATTR}>添加</button>
</div>`
}

/** 日期列的展示：`null`（不过期）⇒「不过期」，否则 `YYYY-MM-DD HH:mm`。 */
export function pkgExpiresText(value: string | null): string {
  if (value === null) return '不过期'
  return value.replace('T', ' ').slice(0, 16)
}

/**
 * 现有包列表的**一行**（纯函数；key_id 已是后端掩码，原样展示）。
 *
 * ⚠️ **来自服务端的文本一律过 `escapeHtml`**（同族纪律 `self-registration.ts::rowHtml`）：
 * 本函数的返回值走 `innerHTML`，而 `key_id` / 备注 / 有效期是用户可控串
 * （备注和密钥 Id 就是表单里自己敲的）⇒ 不转义就是存储型 XSS。
 * 余量两档是 number、启用态是本仓常量——与同族的 `row.id` / 徽章 `style.label` 同口径不转义。
 */
export function pkgRowHtml(row: RealNamePackageRow): string {
  return (
    `<tr class="${PKG_ROW_CLASS}">` +
    `<td>${escapeHtml(row.key_id)}</td>` +
    `<td>${row.total_quota - row.used_quota}/${row.total_quota}</td>` +
    `<td>${row.is_active ? '启用' : '停用'}</td>` +
    `<td>${escapeHtml(pkgExpiresText(row.expires_at))}</td>` +
    `<td>${escapeHtml(row.remark || '（无备注）')}</td>` +
    `</tr>`
  )
}

/**
 * 弹窗里「读取失败 / 空列表」的那一行（空态要说出来，⛔ 不是留一块空白）。
 *
 * ⚠️ `text` 可能是**后端报错回显**（`renderPackageRows` 的 catch 分支把 `e.message`
 * 原样递进来）⇒ 转义收口在**本函数里**，两个调用点（常量空态 / 报错回显）都不用再想这件事。
 */
export function pkgEmptyRowHtml(text: string): string {
  return `<tr><td colspan="5" style="text-align:center;color:#999">${escapeHtml(text)}</td></tr>`
}

/* ────────────────────────────── 列定义（逐条照抄 JS L40-100） ────────────────────────────── */

/** 列的宽松类型（`templet` 是函数，其余是 layui 的键值）。 */
export type RealNameCol = Record<string, unknown>

/**
 * 「认证方式」徽章（对标 JS **L66-72**）：
 * `id5 == 1`（= 我们的 `cert_method === 'basic'`）⇒ **绿**徽章「基础认证」；
 * 否则 ⇒ **橙**徽章「人脸加强」。
 *
 * ⚠️ 映射方向照抄 `if (id5 == 1) { 绿 } else { 橙 }`——**默认值是橙的那一支**，
 * ⛔ 不要「顺手」改成「认不出就绿」：那会把未知值渲染成「基础认证」这个**事实断言**。
 * ⚠️ 我们只会出现 `basic`（`face` 不可达，spec §13-2-1），但这一支**留着一比一渲染**。
 */
export function certMethodBadge(value: unknown): string {
  if (value === 'basic') return '<span class="layui-badge layui-bg-green">基础认证</span>'
  return '<span class="layui-badge layui-bg-orange">人脸加强</span>'
}

/**
 * 「认证结果」徽章（对标 JS **L74-81**）：通过 ⇒ **绿**徽章「通过认证」；
 * 否则 ⇒ **灰**徽章（`<span class="layui-badge">`，「未通过认证」）。
 */
export function certResultBadge(value: unknown): string {
  if (value === 'pass') return '<span class="layui-badge layui-bg-green">通过认证</span>'
  return '<span class="layui-badge">未通过认证</span>'
}

/** 「年龄」列的红字判据（对标 JS **L59**：`res.id3 < 18 || res.id3 > 45`）。 */
export function isAbnormalAge(age: number | null | undefined): boolean {
  // ⚠️ `null` / `undefined` **不算异常**：两种取值（那一格显示空）都不该被说成「年龄异常」。
  // ⛔ 别写成 `Number(age)`：`Number(null) === 0` ⇒ 0 < 18 ⇒ **每一行没算出来的都会被标红**。
  if (age === null || age === undefined) return false
  return age < 18 || age > 45
}

/** 年龄单元格的 `lay-tips` 文案（逐字照抄 JS L60 的属性值）。 */
export const ABNORMAL_AGE_TIPS = '年龄异常，请谨慎录用'

/**
 * 身份证号列的 `title` 文案（逐字照抄对标 JS **L49**）。
 * ⚠️ 这句是给用户看的**事实说明**：我们那一格的值确实是脱敏的
 * （后端复用 `payloads._mask_pii`，前 3 后 2），所以这句话不是"承诺"，是现状。
 */
export const ID_CARD_TITLE =
  '不予完整展示，完整证件号请进入系统用户/用户管理，编辑子账号资料里查看'

/** 认证图片那一格的容器 `title`（逐字照抄 JS L97 的 `title="认证图片只保存1年"`）。 */
export const CERT_IMAGE_TITLE = '认证图片只保存1年'

/**
 * 10 个标题列（+ 1 个勾选列），宽度/对齐/`unresize` **逐条照抄** `RnameCertLogAndSetting.js`
 * **L40-100**。
 *
 * ⚠️ 勾选列的 `type: 'checkbox'` **必须显式声明**：漏了它整列会静默变成普通列
 * （表头勾选框消失、宽度被二次分配，而 `parity` 的空文案特殊列不报差异）。
 * `sort: false` / `unresize: false` 是 layui 的默认值 ⇒ 不写（照本仓既有口径）。
 * `unresize: true` **只有「性别」列**（L54）与勾选列。
 * 全部列都有 `width`（无自动列）；本页 `#tableList` 也**没有**操作列（`cols` 里没有 `toolbar` 列）。
 */
export function buildCols(): RealNameCol[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'userName', title: '系统用户名称', width: 150, align: 'center' },
    { field: 'realName', title: '真实姓名', width: 120, align: 'center' },
    {
      field: 'idCardNo',
      title: '身份证号',
      width: 130,
      align: 'center',
      // ⚠️ **不**手动 `escapeHtml`：layui 2.13.9 的 `table.config.escape` 默认 `!0`（true）
      //（读码 `public/layui/layui.js` 的 `a.prototype.config={limit:10,loading:!0,escape:!0,…}`）
      // ⇒ `templet` 的返回值会被框架**整体转义**。这里再转一次就是**双重转义**
      //（`&` → `&amp;` → `&amp;amp;`），而身份证号里不会出现 `<`，所以那个差异平时看不见、
      // 只在真的出现特殊字符时才变成一串实体。
      templet: (d: Row): string =>
        `<span title="${ID_CARD_TITLE}">${d.idCardNo ?? ''}</span>`,
    },
    // 「性别」是**唯一** `unresize: true` 的数据列（JS L54）。
    { field: 'gender', title: '性别', width: 60, align: 'center', unresize: true },
    {
      field: 'age',
      title: '年龄',
      width: 60,
      align: 'center',
      // 对标 JS L57-63：默认 `<span class="">值</span>`，`<18 || >45` 时换红字 + `lay-tips`。
      templet: (d: Row): string =>
        isAbnormalAge(d.age)
          ? `<span style="color:#ff5722" lay-tips="${ABNORMAL_AGE_TIPS}">${d.age}</span>`
          : `<span class="">${d.age ?? ''}</span>`,
    },
    {
      field: 'certMethod',
      title: '认证方式',
      width: 75,
      align: 'center',
      templet: (d: Row): string => certMethodBadge(d.certMethod),
    },
    {
      field: 'certResult',
      title: '认证结果',
      width: 100,
      align: 'center',
      templet: (d: Row): string => certResultBadge(d.certResult),
    },
    { field: 'appliedAt', title: '申请时间', width: 130, align: 'center' },
    { field: 'certifiedAt', title: '认证时间', width: 130, align: 'center' },
    {
      field: 'certImage',
      title: '认证图片',
      width: 90,
      align: 'center',
      // 对标 JS L96-99：一个蓝链 `color:#31bdec` + `title="认证图片只保存1年"` +
      // `data-img`（图地址）/ `data-uname`（用来拼 photos 的标题）。
      // ⚠️ `data-img` 放**图地址**、`data-uname` 放**系统用户名称**（对标读的就是这两个）。
      // ⚠️ 两者都来自服务端（用户可改的昵称 / 图地址）且这里是**属性拼接** ⇒ 一律过 `escapeHtml`（L19）。
      templet: (d: Row): string =>
        `<a href="javascript:void(0)" style="color:#31bdec" title="${CERT_IMAGE_TITLE}"` +
        ` class="${CERT_IMAGE_CLASS}" data-img="${escapeHtml(d.certImage ?? '')}"` +
        ` data-uname="${escapeHtml(d.userName ?? '')}">点击查看</a>`,
    },
  ]
}

/** 导出给单测对拍表头序列（与页面真正交给 layui 的那一份**同一个出处**）。 */
export const COLS_FOR_TEST: RealNameCol[] = buildCols()

/**
 * `table.render` 里**不随每次取数变化**的那部分。
 *
 * 单测钉它、`mount` 里 `{...TABLE_OPTIONS, where, parseData, done}` 展开它 ⇒ 这几条**静默出错**的键
 * （`toolbar` / `defaultToolbar` / `page` / `request`）不是「只在测试里对」，就是线上那一份。
 */
export const TABLE_OPTIONS: Record<string, unknown> = {
  elem: TABLE_ELEM,
  // ⚠️ 服务端分页必须走 layui 的 `url`：`data` 模式只会在本地分页当前这一页的数组，
  // 「共 N 条 / 总页数」全错。对标的 `url` 是 `/UserAjax/GetUserRnameCertLogList?…`，这里换我们自己的。
  // ⛔ **`url` 上不许再拼任何查询串**（哪怕首屏真发的是哪几个参数）：layui 的取数是
  //    `$.extend({page,limit}, where)` 之后连 `url` 已有的查询串一起送出 ⇒ 拼在 `url` 上会与
  //    `where` 里的**同名键重复**（`?sdate=A&sdate=A&…`，实际发出两条同名查询参数）。
  //    「首屏发哪几个参数」这件事的**唯一出口**是 `where`（见 `table.render` 那处 + `currentWhere`）。
  url: '/api/accounts/realname-logs/',
  method: 'get',
  // ⚠️ **layui 默认发 `page` / `limit`**，而后端分页器认的是 `page` / **`limit`**
  //（`RealNameLogPagination.page_size_query_param = "limit"`，不是 `EnvelopePagination` 的
  // `page_size`）⇒ 与账号列表页**不同名**。这里显式写出来，免得将来有人照 `system-systemlog.ts`
  // 抄成 `{pageName:'page', limitName:'page_size'}`（那会让「每页 20 条」静默失效、后端按默认值走）。
  request: { pageName: 'page', limitName: 'limit' },
  toolbar: TOOLBAR_ELEM,
  defaultToolbar: [...DEFAULT_TOOLBAR],
  page: { limit: PAGE_LIMIT, limits: [...PAGE_LIMITS] },
  cols: [buildCols()],
  text: { none: '暂无数据' },
}

/* ────────────────────────────── 返回值形状 ────────────────────────────── */

/** 后端响应的信封形状（`parseData` 吃的那一份；`data` = `EnvelopePagination` 的载荷）。 */
export type RealNameLogEnvelope = {
  code: number
  msg: string
  data?: {
    items?: RealNameLogRow[]
    total?: number
    page?: number
    page_size?: number
  }
}

/** 页面闭包里的行形状（`field` 指向的键；接口行在这里映射一次）。 */
export type Row = {
  userName: string
  realName: string
  idCardNo: string
  gender: string
  age: number | null
  certMethod: RealNameCertMethod
  certResult: string
  appliedAt: string
  certifiedAt: string
  certImage: string
}

/**
 * 接口行 → 列数据（10 个列字段）。
 *
 * ⚠️ 时间那一格过 `formatCreatedAt`（本仓既有约定，来自 `@/api/auditLogs`）：
 * 后端回的是 DRF 的 ISO-8601（`applied_at` / `certified_at`），而对标那两列渲染的是它服务端
 * **已格式化**好的值 ⇒ 照本仓既有口径格式化成 `YYYY-MM-DD HH:mm:ss`。
 * ⚠️ `certified_at` 为 `null`（还没拿到厂商结果）⇒ **空串**，⛔ 不写 `—` / 「未认证」这类占位
 * （没有值就说没有；对标那格也是空）。
 */
export function toRow(item: RealNameLogRow): Row {
  return {
    userName: item.user_name ?? '',
    realName: item.name ?? '',
    idCardNo: item.id_card_no ?? '',
    gender: item.gender ?? '',
    // ⚠️ `age` 保持 `null` 原样（⛔ 不要 `?? 0`：那造出一个「0 岁」的假事实，见 `isAbnormalAge`）。
    age: item.age ?? null,
    certMethod: item.cert_method,
    certResult: item.result ?? '',
    appliedAt: formatCreatedAt(item.applied_at),
    certifiedAt: item.certified_at ? formatCreatedAt(item.certified_at) : '',
    certImage: item.cert_image ?? '',
  }
}

/* ────────────────────────────── 筛选栏与日期默认值 ────────────────────────────── */

/** 筛选栏里那几个控件的值（**DOM 的 name/id**：`sDate`/`eDate`/`certStatus`/`key1`/`key2`/`key3`）。 */
export type FilterValues = {
  sDate?: string
  eDate?: string
  certStatus?: string
  key1?: string
  key2?: string
  key3?: string
}

/**
 * 「实名时间」的默认区间：**近 30 天** = `today − 29 天` ~ `today`（`YYYY-MM-DD`，本地时区）。
 *
 * ⭐ **业务方拍板**（spec §14-P7 / §15：「近 30 天」，`today-29d` 到 `today`）。
 * ⛔ **不照抄**证据件里那对 `value="2026-08-20"` / `"2026-09-20"` —— 那是**对标服务端按取数当天**
 * 渲染的死值（快照日 2026-09-20），照抄会让「快照日之后核验的记录默认看不见」。
 * ⇒ 这是**有意偏离**，已登记进 `docs/FRONTEND-REPLICA-STANDARD.md`。
 *
 * ⚠️ 算式用 `@/layui-island/dateRange` 的 `dayRangeDaysAgo`（**机制**只此一份，
 * 与交易订单操作记录页的「减 1 个自然月」同一族）；本页的**偏移**是 30 天、只有这一处定义。
 */
export function defaultRealNameDateRange(now: Date = new Date()): DateRange {
  return dayRangeDaysAgo(29, now)
}

/**
 * 读筛选栏（**DOM 名原样**）。
 *
 * ⚠️ 不用 `form.val('form')`：证据件的 `<form class="layui-form">` **没有 `lay-filter`**
 *（`form.val` 要求 `form[lay-filter="form"]`，本页恒取不到 —— 与交易订单操作记录页同一处置）
 * ⇒ 直接读 DOM。
 */
export function readFilterValues(root: HTMLElement): FilterValues {
  return {
    sDate: root.querySelector<HTMLInputElement>('input#sDate')?.value ?? '',
    eDate: root.querySelector<HTMLInputElement>('input#eDate')?.value ?? '',
    certStatus: root.querySelector<HTMLSelectElement>('select#certStatus')?.value ?? '',
    key1: root.querySelector<HTMLInputElement>('input#key1')?.value ?? '',
    key2: root.querySelector<HTMLInputElement>('input#key2')?.value ?? '',
    key3: root.querySelector<HTMLInputElement>('input#key3')?.value ?? '',
  }
}

/**
 * 筛选栏（DOM 名）→ 接口参数（`RealNameLogParams`；线上形状的唯一出口在
 * `api/realname.ts::buildRealNameLogParams`）。
 *
 * ⚠️ 控件名与参数名**不是一一对应**：控件是 `certStatus`（证据件 L86 的 `name`），
 * 而**取数参数**是 `status`（对标 JS L151 读的也是 `#certStatus` 的值、L154 拼进 `status=`）。
 * 这条对应关系**只在这里写一次**，⛔ 不许在别处再拼一遍查询参数。
 */
export function toLogParams(values: FilterValues, page?: number): RealNameLogParams {
  return {
    sdate: (values.sDate ?? '').trim(),
    edate: (values.eDate ?? '').trim(),
    status: (values.certStatus ?? '').trim(),
    key1: (values.key1 ?? '').trim(),
    key2: (values.key2 ?? '').trim(),
    key3: (values.key3 ?? '').trim(),
    ...(page === undefined ? {} : { page, limit: PAGE_LIMIT }),
  }
}

/* ────────────────────────────── 配置弹层的 4 项 ────────────────────────────── */

/**
 * 配置弹层里那四项与证据件字段名（`CertSwicth` / `CertPassRecOrder` / `CertOnineSett` /
 * `CertMinAge`+`CertMaxAge`）的对应表。
 *
 * ⚠️ 键是**证据件里的 `name` 属性**（弹层标记本身就是证据件，本页**不去重写它**）——
 * 回填与取值都按这些 `name` 走，所以这份映射是「弹层 ↔ 接口」的**唯一**接线处。
 * ⛔ 不改证据件里那四个 `name`（它们是对标的原文）。
 */
export const SETTING_RADIO_NAMES = {
  enabled: 'CertSwicth',
  block_claim_without_cert: 'CertPassRecOrder',
  require_cert_for_settlement: 'CertOnineSett',
} as const
/** 两个年龄输入框的 `name`（证据件原文）。 */
export const SETTING_MIN_AGE_NAME = 'CertMinAge'
export const SETTING_MAX_AGE_NAME = 'CertMaxAge'

/** 对标那三颗 radio 的取值：`'Y'` = 启用/不允许/要求，`'N'` = 反面（证据件 `value="N"`/`"Y"`）。 */
const RADIO_YES = 'Y'
const RADIO_NO = 'N'

/**
 * `RealNameSetting` → 三颗 radio 的取值。
 * ⚠️ **默认值不是本函数定的**：它是证据件的 `checked='checked'`（三颗都在 `N` 上）
 * ⇒ 这一档只在「服务端明确回了别的值」时才会有差异。
 */
export function boolToRadio(value: boolean): string {
  return value ? RADIO_YES : RADIO_NO
}

/** 三颗 radio 的取值 → 布尔（`'Y'` 才算真；认不出的取值按**假**处理 —— 配置的默认值就是假）。 */
export function radioToBool(value: string | undefined): boolean {
  return value === RADIO_YES
}

/**
 * 「年龄」输入框的取值 → 整数。
 *
 * ⚠️ **`0` 是合法值**（`0` = 不限制，spec §4.2 的列默认值就是 `0`）
 * ⇒ 判据是**空串**而不是真值：`''` ⇒ `0`（空 = 不限制）、`'0'` ⇒ `0`、`'30'` ⇒ `30`。
 * ⛔ 别写成 `Number(value) || 0`——那恰好也回 0，但**它把「非法输入」与「合法 0」混成一个**；
 * 也⛔ 别把 `0` 当成「没填」清空（`AGENTS.md` §5-E / E-0021 与 E-0074 的判别口诀）。
 */
export function ageInputToNumber(value: string | undefined): number {
  const raw = (value ?? '').trim()
  if (raw === '') return 0
  const n = Number(raw)
  return Number.isFinite(n) ? n : 0
}

/**
 * 从弹层 DOM 里读那四项（**回填 / 提交共用一份**，⛔ 不写两份）。
 *
 * ⚠️ 找不到控件时按**证据件的默认值**回（三颗 `N` = `false`、两个年龄 `0`），
 * 而不是回 `undefined` —— 后者会让「弹层标记不在页面上」这种事故表现成 PATCH **静默丢掉那几个键**
 * （后端 `PATCH` 的语义是「未传的键保持原值」⇒ 用户改了别的键、这一键不动，看不出任何异常）。
 */
export function readSettingForm(panel: HTMLElement | null): RealNameSettingPatch {
  if (!panel) {
    return {
      enabled: false,
      block_claim_without_cert: false,
      require_cert_for_settlement: false,
      min_age: 0,
      max_age: 0,
    }
  }
  const radio = (name: string): boolean =>
    radioToBool(
      panel.querySelector<HTMLInputElement>(`input[name="${name}"]:checked`)?.value,
    )
  const num = (name: string): number =>
    ageInputToNumber(panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)?.value)
  return {
    enabled: radio(SETTING_RADIO_NAMES.enabled),
    block_claim_without_cert: radio(SETTING_RADIO_NAMES.block_claim_without_cert),
    require_cert_for_settlement: radio(SETTING_RADIO_NAMES.require_cert_for_settlement),
    min_age: num(SETTING_MIN_AGE_NAME),
    max_age: num(SETTING_MAX_AGE_NAME),
  }
}

/**
 * 把服务端的四项配置**回填**进弹层 DOM。
 *
 * ⚠️ `min_age` / `max_age` 用 `String(value)`：`0` 会写成 `"0"`（**合法值**，
 * 证据件的默认 `value="0"` 就是这么写的）⇒ ⛔ 不许把 `0` 当"没填"清成空串
 * （`AGENTS.md` §5-E / E-0021 与 E-0074：清空 = 换掉对标行为，且会在前端的校验门上炸）。
 * ⚠️ 三颗 radio 要同时设置 `checked` **属性**与**属性值**：属性是 `form.render()` 读的那一份
 * （layui 把原生 radio 藏起来、另画一个壳），只改 `input.checked` 而不同步属性时，
 * **用户看到的壳还是旧的**（点开弹层看到的是默认值、保存下去却是服务端的值 —— 静默不一致）。
 */
export function fillSettingForm(panel: HTMLElement | null, setting: RealNameSetting): void {
  if (!panel) return
  const pair: [keyof typeof SETTING_RADIO_NAMES, boolean][] = [
    ['enabled', setting.enabled],
    ['block_claim_without_cert', setting.block_claim_without_cert],
    ['require_cert_for_settlement', setting.require_cert_for_settlement],
  ]
  for (const [key, value] of pair) {
    const name = SETTING_RADIO_NAMES[key]
    const want = boolToRadio(value)
    for (const input of Array.from(
      panel.querySelectorAll<HTMLInputElement>(`input[name="${name}"]`),
    )) {
      const on = input.value === want
      input.checked = on
      if (on) input.setAttribute('checked', 'checked')
      else input.removeAttribute('checked')
    }
  }
  const minInput = panel.querySelector<HTMLInputElement>(`input[name="${SETTING_MIN_AGE_NAME}"]`)
  const maxInput = panel.querySelector<HTMLInputElement>(`input[name="${SETTING_MAX_AGE_NAME}"]`)
  if (minInput) minInput.value = String(setting.min_age)
  if (maxInput) maxInput.value = String(setting.max_age)
}

/* ────────────────────────────── 挂载 ────────────────────────────── */

/**
 * 把服务端回的余量写进购买按钮的文案。
 *
 * ⚠️ 按钮**在 layui 渲染出来的工具栏里**（`.layui-table-tool`），而工具栏模板来自证据件
 * ⇒ 分两层找：先在 `root` 里找现成的，再退到 `document`（layui 可能把工具栏放在别处）。
 * ⚠️ 用 `[lay-event="buyBtn"]` 认按钮（证据件原文的属性），⛔ 不用文案匹配
 * （文案本身就是要改的那个东西，拿它当选择器是循环判据）。
 */
export function renderBuyLabel(root: HTMLElement, remaining: number | null): void {
  const label = buyButtonLabel(remaining)
  const btn =
    root.querySelector<HTMLElement>('[lay-event="buyBtn"]') ??
    document.querySelector<HTMLElement>('[lay-event="buyBtn"]')
  if (btn) btn.textContent = label
}

/** 挂载整页；返回卸载函数。 */
export async function mountRealNamePage(
  root: HTMLElement,
  _ctx: IslandContext,
): Promise<() => void> {
  // ⚠️ 挂载顺序（陷阱 ③）：`bootstrapIsland` 挂样式 → 取证据件 → `root.innerHTML` →
  // `initIslandElements`（它绑的是标记里的页签）。顺序反了会 FOUC / 绑定落空。
  const layui = await bootstrapIsland(['form', 'table', 'layer', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const laydate = layui.laydate as LayuiModule

  // 对标 `<title>` 是服务端渲染的「系统用户实名认证记录与配置」。
  document.title = '系统用户实名认证记录与配置'
  root.innerHTML = await loadConfigArtifact(ARTIFACT_KEY)
  initIslandElements(layui)

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }

  // —— 实名时间：填**近 30 天**（有意偏离证据件的死值，见 `defaultRealNameDateRange`）+ 交给 laydate。
  //    参数照对标 JS L19-26 逐条：两个都是 `type: 'date'`、**没有 `min`/`max`**（本页对标没写）。
  const { sDate, eDate } = defaultRealNameDateRange()
  const sInput = root.querySelector<HTMLInputElement>('input#sDate')
  const eInput = root.querySelector<HTMLInputElement>('input#eDate')
  if (sInput) sInput.value = sDate
  if (eInput) eInput.value = eDate
  ;(laydate.render as (o: unknown) => void)({ elem: '#sDate', type: 'date' })
  ;(laydate.render as (o: unknown) => void)({ elem: '#eDate', type: 'date' })

  /**
   * 当前筛选对应的**线上参数**（唯一形状出口）。
   *
   * ⚠️ 这里**刻意**不设一个「有没有点过搜索」的开关变量：那个开关是**错的口径**（见下方
   * `reload` 与「刷新」那一支的长注释）。这一发要不要带筛选，完全由调用方给的
   * `includeFilters` 决定 —— **首屏那一次**传 `false`、**其余每一次**传 `true`。
   */
  const currentWhere = (
    page = 1,
    includeFilters = false,
  ): Record<string, string | number> =>
    buildRealNameLogParams(toLogParams(readFilterValues(root), page), includeFilters)

  /**
   * 重新取数。默认回到第 1 页（搜索/刷新都是「重看一遍」）。
   *
   * ⭐ **任何一次 `reload` 都带全部 6 项筛选**（`includeFilters = true` 写死在这里，⛔ 没有开关）。
   * 它覆盖了**两个**触发点：「搜索」（`submit(seachBtn)`）与「刷新列表」（`toolbar` 的 `refresh`）。
   *
   * 对标出处：`RnameCertLogAndSetting.js` 的 `reloadOrderList()`（**L147-154**）——它**每次都**从
   * DOM 现读 `sdate`/`edate`/`status`/`key1`/`key2`/`key3` 再拼进 `seachParam`，而
   * 「刷新」（**:115-117**）与「搜索」（**:146**）调的是**同一个函数** ⇒ **两边都带筛选**。
   *
   * ⚠️ **这不是我们的发明，是对标口径**（`AGENTS.md` §5-D）。曾经的写法是「没点过搜索就只发日期」
   * ——那会让「用户改了筛选栏、直接点刷新」与对标**在可见行为上不一致**（对标带筛选、我们不带），
   * 且当时没登记成偏离。⭐ 与对标一致的**只有**这一处：首屏那一次仍只发 `sdate`/`edate`
   * （对标首屏 **L27,35** 只拼了两个日期），那半由 `table.render` 的 `where: currentWhere()`
   * 用默认的 `includeFilters = false` 承担。
   */
  function reload(page = 1): void {
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      where: currentWhere(page, true),
      page: { curr: page },
    })
  }

  /* ── 配置弹层（`.settingDiv`） ─────────────────────────────────────────── */

  let settingsLayer: number | undefined

  /**
   * 弹层容器。⚠️ 用**文档级**选择器：`layer.open` 会把容器搬进 `.layui-layer-content`。
   *
   * ⛔ **这个文档级分支不是防御性写法、是承重逻辑**：真实 layui 的 `layer.open({type:1})` 会把
   * `content` 那个节点（= `root` 里的 `.settingDiv`）**搬进 `document.body` 下的
   * `.layui-layer-content`** ⇒ 弹层一开，`root` 里就**再也没有**它了。真浏览器里**只有**
   * 这一句能找到面板，`root` 那一句是**逐字复刻期（还没开过层）**才命中的兜底。
   * ⚠️ 去掉它的后果**不落在画面上、也不报错**：`readSettingForm(null)` 会回**证据件的默认值**
   * （三颗 `N` + 两个 `0`）⇒ 「保存设置」把**整份默认配置** PATCH 上线（静默覆盖线上配置）。
   * 判据：`__tests__/user-rnamecertlogandsetting.spec.ts` 那条「把面板搬进 `body` 的
   * `.layui-layer-content` 之后再保存」的用例——删掉这一句它**必须红**。
   */
  function findSettingsPanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>(`.layui-layer-content > ${SETTINGS_PANEL_SELECTOR}`) ??
      root.querySelector<HTMLElement>(SETTINGS_PANEL_SELECTOR)
    )
  }

  /**
   * 往弹层底部追加那行**中立提示**（业务方拍板，spec §14-P11 / §15）。
   *
   * ⚠️ **运行期追加**，⛔ 不改证据件：证据件是对标渲染结果的留档，改它等于伪造证据
   * （先例：`user-myinfo.ts::removeReadonlyBindingNote` / `system-homesystemsetting.ts`
   * 删弹窗里那条「注意」）。
   * ⚠️ 幂等：`layer.open` 关掉之后容器会被 layui 放回原位，第二次开层不能再插一行。
   * ⚠️ 挂在**容器里**（不是 `.layui-layer-content` 里）：容器才是被借给弹层的那一块，
   * 挂在外面的话 `layer.close` 后提示就跟着丢了。
   * 判据：`__tests__/user-rnamecertlogandsetting.spec.ts` 同时钉「运行期有这一行」与
   * 「证据件里没有这一行」**两半**——单独一半都能被「顺手改证据件」蒙过去。
   */
  function ensureNotAppliedNote(panel: HTMLElement): void {
    if (panel.querySelector('[data-realname-note]')) return
    const note = document.createElement('div')
    note.setAttribute('data-realname-note', '')
    // 视觉值取**本页已有的那一份**（证据件里弹层各块的 `margin-left:10px` + `#999` 灰字），
    // ⛔ 不自造一套配色/字号（本页没有这条的对标证据 ⇒ 用页内既有值最不容易偏）。
    note.setAttribute('style', 'margin:10px 10px 0;color:#999;line-height:20px')
    note.textContent = SETTINGS_NOT_APPLIED_NOTE
    panel.appendChild(note)
  }

  /**
   * 开配置弹层：**先取真值回填、再开层**。
   *
   * ⚠️ 顺序是判据的一部分：先开层再异步回填，用户会先看到**证据件的默认值**
   * （三颗 `N` / 两个 `0`）、再被服务端的值刷一遍 —— 那个窗口里点「保存设置」就会把
   * **默认值**写回去（静默覆盖线上配置）。所以这里 `await` 完再 `layer.open`。
   * ⚠️ 取数失败**不开层**、只给一句可读提示：开一个「全是默认值」的层比不开更糟
   *（用户会以为线上就是这样、然后拿错误的值去保存）。
   */
  async function openSettings(): Promise<void> {
    let setting: RealNameSetting
    try {
      setting = await getRealNameSetting()
    } catch (e) {
      msg(e instanceof Error ? e.message : '实名认证设置读取失败')
      return
    }
    const panel = findSettingsPanel()
    if (!panel) {
      // 证据件里那个容器不见了 —— 那是**标记层**的事故，不能静默（就当取数失败处理）。
      msg('实名认证设置面板未渲染，请刷新页面重试')
      return
    }
    fillSettingForm(panel, setting)
    ensureNotAppliedNote(panel)
    // ⚠️ layui 的 `form.render()` 必须重跑：三颗 radio 的**壳**是按属性画的，
    //    改了 `checked` 属性不同步壳 ⇒ 用户看到的还是旧的那一颗。
    ;(form.render as (type?: string) => void)('radio')
    settingsLayer = (layer.open as (o: unknown) => number)({
      // 标题逐字照抄对标 JS L117。
      title: '实名认证相关设置',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 3,
      // 对标 L123 是 `area: [isPc ? '620px' : '90%', '480px']`；本页**只做桌面值**
      // （本仓既有的 isPc 处置：只给桌面值，⛔ 不造「层全屏、列桌面」的半吊子，
      // 见 `docs/FRONTEND-REPLICA-STANDARD.md` 的已知差异表）。
      area: ['620px', '480px'],
      content: iframeSafeContent(layui, panel),
    })
  }

  /**
   * `layer.open` 的 `content` 必须是 **jQuery 对象**（传裸 `HTMLElement` 会让 layui 抛
   * `TypeError: r.parents is not a function`、弹层**根本不出来**）——对标 JS L124 写的就是
   * `content: $('.settingDiv')`。这里用 layui 自带的 `$`（island 里没有独立 jquery，
   * 先例 `system-user-list.ts` 的 `$` 注释）。
   */
  function iframeSafeContent(layuiNs: unknown, node: HTMLElement): unknown {
    const jq = (layuiNs as { $?: (el: Element) => unknown }).$
    return typeof jq === 'function' ? jq(node) : node
  }

  /**
   * 「保存设置」：读弹层里那四项 → `PATCH`。
   *
   * ⚠️ 绑在**挂载时**（⛔ 不是像对标那样藏在「点击查看」的点击回调里 —— 见文件头那条
   * 「不照抄对标缺陷」）。
   * ⚠️ 载荷 = `readSettingForm` 的**五个键全给**：后端 `PATCH` 的语义是「未传的键保持原值」，
   * 而这里是一份**完整表单**（用户看到什么就提交什么）⇒ 五个键都要到，
   * 否则「取消勾选启用」这类改动会**静默不生效**（缺席 = 不改）。
   */
  async function saveSettings(): Promise<void> {
    const payload = readSettingForm(findSettingsPanel())
    try {
      await updateRealNameSetting(payload)
    } catch (e) {
      msg(e instanceof Error ? e.message : '保存失败')
      return
    }
    msg('保存成功')
    if (settingsLayer !== undefined) {
      ;(layer.close as (i: number) => void)(settingsLayer)
      settingsLayer = undefined
    }
  }

  /* ── 配置实名资源包弹窗（T3，spec §2.4；⛔ 2026-10-10 的「只弹一句」toast 路径已删） ── */

  // ⚠️ 不记层索引：本弹窗没有程序化关层的需求（点 X / 点遮罩 / 添加成功留层看新包都行），
  // ⛔ 别为了对称给 `openSettings` 之外的层塞一个无人读取的索引。
  let packagesPanel: HTMLElement | null = null
  let pkgSubmitting = false

  /**
   * 弹层容器。⚠️ 用**文档级**选择器（与 `findSettingsPanel` 同一条纪律）：`layer.open`
   * 会把容器搬进 `.layui-layer-content`，弹层一开 `root` 里就没有它了。
   */
  function findPackagesPanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>(
        `.layui-layer-content > ${PACKAGES_PANEL_SELECTOR}`,
      ) ?? root.querySelector<HTMLElement>(PACKAGES_PANEL_SELECTOR)
    )
  }

  /** 现有包列表的取数与铺开（唯一的取数出口；空态与失败都落在一行里说清楚）。 */
  async function renderPackageRows(tbody: HTMLElement | null): Promise<void> {
    if (!tbody) return
    try {
      const rows = await listRealNamePackages()
      tbody.innerHTML =
        rows.length > 0 ? rows.map(pkgRowHtml).join('') : pkgEmptyRowHtml('还没有录入任何密钥包')
    } catch (e) {
      tbody.innerHTML = pkgEmptyRowHtml(
        e instanceof Error ? e.message : PKG_LIST_FAILED_TEXT,
      )
    }
  }

  /** 清空「添加密钥包」表单。⛔ 每个框都清：添加成功后表单回到空白，不是留着旧值。 */
  function clearPackageForm(panel: HTMLElement): void {
    for (const name of Object.values(PKG_INPUT_NAMES)) {
      const el = panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)
      if (el) el.value = ''
    }
  }

  /**
   * 提交「添加密钥包」。
   *
   * ⚠️ **secret 框在任何一次提交后都清空**（success / 失败都清，`finally` 落点）：
   * 「提交后清空、任何响应不回显」是 spec §2.4 的判据——失败时留着它重试虽然省事，
   * 但那会让最贵的字符串在 DOM 里活过一次失败的往返。
   * ⚠️ 其余四框只在**成功**后清（失败时用户改的是别的字段，不必重填）。
   */
  async function submitPackage(): Promise<void> {
    const panel = findPackagesPanel()
    if (!panel || pkgSubmitting) return
    const val = (name: string): string =>
      (panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)?.value ?? '').trim()
    const expiresAt = val(PKG_INPUT_NAMES.expiresAt)
    const remark = val(PKG_INPUT_NAMES.remark)
    pkgSubmitting = true
    try {
      await createRealNamePackage({
        key_id: val(PKG_INPUT_NAMES.keyId),
        key_secret: val(PKG_INPUT_NAMES.keySecret),
        total_quota: Number(val(PKG_INPUT_NAMES.totalQuota)),
        ...(expiresAt === '' ? {} : { expires_at: expiresAt }),
        ...(remark === '' ? {} : { remark }),
      })
      msg(PKG_CREATED_TEXT)
      clearPackageForm(panel)
      // 成功后刷新列表（spec §2.4：掩码 / 余量 / 状态马上能看见新包）。
      await renderPackageRows(panel.querySelector<HTMLElement>('[data-pkg-rows]'))
    } catch (e) {
      // ⛔ 信封 msg 展示给用户（重复 key-id ⇒ 命令那句文案），不自编。
      //    ⚠️ `layer.msg` 把文案当 **HTML** 渲染，而后端报错里可能带着**用户输入的回显**
      //    （如 `parse_expires_at` 的 `{raw!r}`）⇒ 过 `escapeHtml`（同族 XSS 纪律；
      //    不含特殊字符的文案转义前后逐字相同，既有用例的原文断言不受影响）。
      msg(e instanceof Error ? escapeHtml(e.message) : '添加失败')
    } finally {
      const secret = panel.querySelector<HTMLInputElement>(
        `input[name="${PKG_INPUT_NAMES.keySecret}"]`,
      )
      if (secret) secret.value = ''
      pkgSubmitting = false
    }
  }

  /**
   * 开配置弹窗：**先取数铺列表、再开层**（与 `openSettings` 同一条顺序纪律——
   * 先开层再异步回填，用户会先看到空白/旧数据再被刷一遍）。
   */
  async function openPackages(): Promise<void> {
    if (!packagesPanel) {
      packagesPanel = document.createElement('div')
      packagesPanel.innerHTML = buildPackagesPanelMarkup()
      const panel = packagesPanel.firstElementChild as HTMLElement
      // 「添加」按钮绑**一次**：层开关只是搬节点，监听器在按钮上一直有效。
      panel
        .querySelector<HTMLElement>(`[${PKG_SUBMIT_ATTR}]`)!
        .addEventListener('click', () => void submitPackage())
      packagesPanel = panel
    }
    const panel = packagesPanel
    if (!panel.isConnected) root.appendChild(panel)
    await renderPackageRows(panel.querySelector<HTMLElement>('[data-pkg-rows]'))
    ;(layer.open as (o: unknown) => number)({
      title: '配置实名资源包',
      shade: [0.3, '#333'],
      shadeClose: true,
      type: 1,
      skin: 'layui-layer-lan',
      anim: 3,
      // 与设置弹层同族（对标 L123 的桌面值口径；本页不造「层全屏」的半吊子）。
      area: ['620px', '560px'],
      content: iframeSafeContent(layui, panel),
    })
  }

  /* ── 认证图片（`a.viewCertImg`） ───────────────────────────────────────── */

  /**
   * 点「点击查看」：**先弹法律提示原文** ⇒ 用户在提示里点「我已知晓，保证不滥用」才 `layer.photos`。
   *
   * 对标出处：JS **L165-189**（`$(document).on('click', '.viewCertImg', …)` 的 `layer.confirm`
   * 与两个按钮，回调里才 `layer.photos`）。
   * ⚠️ `cert_image` 本批**恒空**（spec §13-2-4）⇒ 空值时给一句「暂无认证图片」、**不置灰**
   * （`AGENTS.md` §5-D：无后端的部分照对标渲染 + 点了要有可见反应）。
   */
  function onCertImageClick(link: HTMLElement): void {
    const img = link.dataset.img ?? ''
    const userName = link.dataset.uname ?? ''
    // ⚠️ **显式持有提示框的层索引**（对标在 `yes` 回调里 `layer.close(tipsLayer)` 用的是同一个
    //    变量）。⛔ 不依赖 layui「点了按钮自动关层」的默认行为：那个默认行为**只管按钮、
    //    不管回调里再开一个层**，而且我们这里要在回调里 `layer.photos`。
    let tipsLayer: number | undefined
    tipsLayer = (layer.confirm as (
      t: string,
      o: unknown,
      yes: () => void,
      no?: () => void,
    ) => number)(
      CERT_IMAGE_NOTICE,
      { btn: [...CERT_IMAGE_NOTICE_BUTTONS] },
      () => {
        if (tipsLayer !== undefined) {
          ;(layer.close as (i: number) => void)(tipsLayer)
          tipsLayer = undefined
        }
        if (img === '') {
          msg(CERT_IMAGE_EMPTY_TEXT)
          return
        }
        ;(layer.photos as (o: unknown) => void)({
          photos: {
            // ⚠️ vendored `layer.photos` 用 `'<img src="'+src+'" alt="'+alt+'">'` 拼 HTML
            //    （`public/layui/layui.js`，读码）⇒ 标题 / alt / src 都要过 `escapeHtml`（L19）。
            title: `${escapeHtml(userName)}的认证图`,
            start: 0,
            data: [{ alt: `${escapeHtml(userName)}的认证图片`, pid: 0, src: escapeHtml(img) }],
          },
          footer: false,
        })
      },
      // 「关闭」那一支：对标给了个空回调（JS L186-187）⇒ 照抄「什么都不做」。
      () => {},
    )
  }

  /* ── 工具栏三颗（`table.on('toolbar(tableList)')`，对标 JS L109-141） ────── */

  ;(table.on as (e: string, cb: (obj: { event?: string }) => void) => void)(
    `toolbar(${TABLE_FILTER})`,
    (obj) => {
      switch (obj.event) {
        case 'refresh':
          // 刷新列表：**读当前 DOM 里的筛选值、全部带上**，再重取（⛔ 不写成「没点过搜索就只发日期」）。
          //
          // ⭐ 对标 `RnameCertLogAndSetting.js` 的 `reloadOrderList()`（**L147-154**）就是**每次**
          //    从 DOM 现读 `sdate`/`edate`/`status`/`key1`/`key2`/`key3` 再拼 `seachParam`，
          //    而「刷新」（**:115-117**）与「搜索」（**:146**）走的是**同一个函数** ⇒ 刷新**也带筛选**。
          // ⚠️ **首屏那一次仍只发 `sdate`/`edate`**（对标首屏 **L27,35** 只拼了两个日期）——
          //    那半保留在 `table.render` 的 `where` 上（`currentWhere()` 的默认 `includeFilters:false`）。
          // ⚠️ 这条是**与对标一致**、⛔ 不是我们的有意偏离 ⇒ `docs/FRONTEND-REPLICA-STANDARD.md`
          //    的偏离表**不需要**为它新增条目（R3-1 的整改结论）。
          reload(1)
          break
        case 'settingBtn':
          void openSettings()
          break
        case 'buyBtn':
          // T3（spec §2.4，业务方拍板改**应用内配置**）：点击打开
          // 「配置实名资源包」弹窗（掩码列表 + 添加密钥包）。2026-10-10 选项 B 的
          // 「只弹一句请联系客服购买」已废（`BUY_CONTACT_TEXT` 与这条 toast 路径同一次删除）。
          void openPackages()
          break
        default:
          // 工具栏只有这三颗（证据件 L136-138）⇒ 别的事件**什么都不做**。
          break
      }
    },
  )

  // —— 「搜索」：`form.on('submit(seachBtn)')`（对标 JS L142-146）。layui 的 form 模块对
  //    `*[lay-submit]` 做了文档级委托，所以这里不需要额外的点击监听。
  ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
    // ⭐ 搜索与刷新走**同一条** `reload`（对标 `:146` 与 `:115-117` 都调 `reloadOrderList`）
    //    ⇒ 这里**没有**任何「升级成全参」的状态要置（`reload` 自己就带全部 6 项）。
    reload(1)
    return false
  })

  // —— 「保存设置」：`form.on('submit(saveSettingBtn)')`（对标 JS L190-216 的**意图**，
  //    但**绑在挂载时**、⛔ 不照抄它「被写在点击查看回调里」的作用域事故，见文件头）。
  ;(form.on as (e: string, cb: () => void) => void)('submit(saveSettingBtn)', () => {
    void saveSettings()
    return false
  })

  // —— 「点击查看」：`root` 上的委托（`artifactPage` 的兜底收口已经没有了 ⇒ 本页自己接管）。
  //    ⚠️ 用**委托**而不是逐个 `addEventListener`：行是 layui 渲染的、翻页后会整批换掉。
  const onClick = (event: Event): void => {
    const link = (event.target as HTMLElement).closest<HTMLElement>(CERT_IMAGE_SELECTOR)
    if (!link || !root.contains(link)) return
    event.preventDefault()
    onCertImageClick(link)
  }
  root.addEventListener('click', onClick)

  // —— 表格渲染：`parseData` 把统一信封翻成 layui 要的 `{code,msg,count,data}`。
  //    对标的 `done`（L105-107）只做了一件事：把当前页记进 `page` 变量。我们用不上它
  //    （layui 的 `page.curr` 由 `table.reload` 自己带）⇒ 不抄那个闭包变量。
  ;(table.render as (o: unknown) => void)({
    ...TABLE_OPTIONS,
    // ⭐ 首屏 = **两段式的第一段**：`includeFilters` 取默认的 `false` ⇒ 请求里只有 `sdate`/`edate`
    //    （对标首屏 JS L27,35 只拼了两个日期）。⛔ 这里**不许**跟着 `reload` 一起升级成全参。
    where: currentWhere(),
    parseData: (res: RealNameLogEnvelope) => ({
      code: res.code,
      msg: res.msg,
      count: res.data?.total ?? 0,
      data: (res.data?.items ?? []).map(toRow),
    }),
  })
  ;(form.render as (type?: string) => void)()

  // —— 余量：`GET /api/accounts/realname-quota/` ⇒ 写进购买按钮的文案。
  //    ⚠️ **由服务端数据渲染**（spec §10.1 第 2 条）：证据件里那个 `0` 是对标的**字面量**，
  //    我们这一格必须是真读数。取不到时**保留证据件的字面量** + 一句提示（⛔ 不静默留 0 装作正常
  //    —— 那正是后端 `remaining=null` 那一档要避免的「以为我没次数了」）。
  //    ⚠️ **`0` 与 `null` 两档必须能区分**（`AGENTS.md` §5-E / `E-0021`）：按钮文案两档相同
  //    （都回落到证据件的字面量，见 `buyButtonLabel`）⇒ 靠这两句**互斥**的提示分家：
  //    `0` ⇒「余量已用完」、`null` ⇒「读不到、请稍后重试」。⛔ 别写成 `if (!remaining)`：
  //    那会把合法的 `0` 与 `null` 判成同一个（正是 `E-0021` 那类缺陷）。
  void getRealNameQuota().then(
    (quota) => {
      renderBuyLabel(root, quota.remaining)
      if (quota.remaining === 0) msg(QUOTA_EXHAUSTED_TEXT)
      else if (quota.remaining === null) msg('暂时读不到本租户的实名认证剩余次数，请稍后重试')
    },
    (e: Error) => {
      // 余量读失败**不让整页失败**：列表与配置都还能用，按钮保留证据件的字面量。
      msg(e.message || '实名认证剩余次数读取失败')
    },
  )

  return () => {
    root.removeEventListener('click', onClick)
    // 只清我们注入的标记；`layer` 的弹层挂在 `document.body`，由 layui 自己收。
    root.innerHTML = ''
  }
}

export default {
  mount: mountRealNamePage,
} satisfies IslandPage

/**
 * `certStatus` 下拉「全部」那一档的取值，**唯一出处是 `@/api/realname`**
 * （后端 `RealNameLogFilterSerializer.status` 的 `ChoiceField(choices=("0","1","2"))`）。
 *
 * 本页**不自己造**这个默认值：证据件的 `<option value="0" selected="selected">全部</option>`
 * 就是它的赋值处，取值路径是「读 DOM」。这里 re-export 一次，是为了让「页面那一档的取值」
 * 与「api 模块的常量」在**同一个符号**上对得上（⛔ 不许在页面里另写一个 `'0'` 字面量）。
 */
export { REALNAME_STATUS_ALL } from '@/api/realname'
