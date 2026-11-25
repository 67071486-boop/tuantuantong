/**
 * 【人员管理】页的 **layui 实现**（对标物 `/User/userlist` 的原样复刻）。
 *
 * 为什么不用 Vue/Element Plus：业务方要「像素级一样」，而对标物是 layui 渲染的。
 * 所以这一页是**一个 layui island**：本模块往容器里注入对标同款标记，
 * 交给 layui 自己的 `form.render` / `table.render` 渲染，交互也用 layui 的事件；
 * 数据仍走我们的 `/api/accounts/`。
 *
 * 与 `UserListView.vue` 的分工：SFC 只负责「挂载/卸载本模块 + 提供路由跳转」。
 */
import {
  deleteAccount,
  fetchAccountRoles,
  listAccounts,
  resetAccountPassword,
  restoreAccount,
  setAccountStatus,
  setAccountsStatus,
  updateAccount,
  type AccountRow,
  type AccountWritePayload,
} from '@/api/accounts'
import { fetchMe, type User } from '@/api/auth'
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { HttpError } from '@/api/errors'
import { fetchAlipayDuplicates, type AlipayDuplicatesResult } from '@/api/performances'
import { setDepositQuotaEnabled, TAKER_DEPOSIT_QUOTA_NODE } from '@/api/takerDepositQuota'
import { backendErrorMessage } from '@/layui/backendError'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'
import { initFormSelects } from '@/layui/vendor'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'
import { canNode } from '../perms'
import {
  BUSINESS_TYPE_FILTER_OPTIONS,
  CERT_FILTER_OPTIONS,
  MEMBER_TYPE_FILTER_OPTIONS,
  REAL_SWITCHES,
  SETTLE_FILTER_OPTIONS,
  USER_STATUS_FILTER_OPTIONS,
  type UserToolbarAction,
} from '@/config/systemUsers'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const ACCOUNTS_URL = `${API_BASE}/api/accounts/`
const TABLE_ID = 'tableList_user'
const TABLE_FILTER = 'tableList_user'

/** 接单统计端点（P3e Task 3，合并 main 带入）：`GET /api/business/order-stats`，参数 `stime`/`etime`/`gameName`/`page`/`limit`。 */
const ORDER_STATS_URL = `${API_BASE}/api/business/order-stats`

/**
 * 接单统计卡片（`.recTotalDiv`）的**类名与选择器分成两个常量**（E-0061 ①）：
 * `class="${PANEL_SELECTOR}"` 会造出 `class=".recTotalDiv"`，选择器永远匹配不到、弹层**静默打不开**。
 */
const REC_TOTAL_PANEL_SELECTOR = '.recTotalDiv'

/** 卡片内表 id（对标 `User__userlist.html` L404 的 `#tableList2`）。 */
const REC_TOTAL_TABLE_ID = 'tableList2'

/**
 * 卡片里那个「游戏权限」下拉的 id 与 `lay-filter`。
 *
 * ⚠️ `id` **必须与主筛选栏不同**：对标同一页有**两个** `id="gameName"`（主筛选栏 L117 + 卡片 L352），
 * `$('#gameName')` 只会取到文档里第一个——对标的卡片下拉在 JS 里只能靠 `lay-filter` 找。
 * 我们保持 id 唯一，取值一律走本 id。
 * ⚠️ `name` 我方**有意偏离对标**、写 `gameNameTotal`（不是 `gameName`）：整页在**一个** `<form>` 里，
 * 同名会让 `form.val('sform')` 读到卡片里的值，把主筛选栏的「游戏权限」筛选**静默换掉**
 * （E-0027 同型）——本分支的 `buildUserListWhere` 真的会读 `gameName`，所以这条不是洁癖。
 */
const REC_TOTAL_GAME_SELECT_ID = 'recTotalGameName'
const REC_TOTAL_GAME_FILTER = 'selectGameTotal'

/** 两个日期输入框的 id（对标 `laydate.render({elem:'#rsDate'})` / `'#reDate'`，L616-630）。 */
const REC_TOTAL_START_ID = 'rsDate'
const REC_TOTAL_END_ID = 'reDate'

/** 卡片表格的分页复刻值（对标 `getRecOrderTotal()`：`limit:100`、`limits:[20,30,50,100,500,1000,2000]`）。 */
const REC_TOTAL_DEFAULT_LIMIT = 100
const REC_TOTAL_LIMITS: readonly number[] = [20, 30, 50, 100, 500, 1000, 2000]

/**
 * 接单统计的行（`OrderStatsListSerializer` / `apps/business/order_stats.py` 的逐字字段）。
 *
 * ⚠️ 两处**约定**别猜：`complateRate` 是**不带 `%` 的数值**（如 `93.98`，`%` 由前端拼）；
 * `lastRecOrderDate` 在窗口内没接过单时后端给的是**字符串「无」**（不是 `null`）。
 * `noSettAmount` 是 JSON number（两位小数是精度、不是「保留两位的字符串」）。
 */
export type OrderStatsRow = {
  idx: number
  uid: number
  userName: string
  orderCount: number
  noSettAmount: number
  complateRate: number
  createUserDate: string
  lastRecOrderDate: string
}

export type UserListPageOptions = {
  onNavigate: (name: string, params?: Record<string, string>) => void
}

/**
 * 工具栏 **14 颗按钮**的**逐字标记**，来自对标 `#toolbarDemo`（`User__userlist.html` **L279-299**，实测）。
 *
 * ⚠️ 对标是 **21 颗**（L279-299）；业务方 2026-09-22 裁掉其中 7 颗，留下的 14 颗见下表
 * （`line` = 该按钮在**对标原文**里的实测行号，被删的 7 颗占 283 / 288 / 289 / 290 / 291 / 292 / 299）。
 *
 * 为什么不再从 `USER_TOOLBAR` 拼字符串：`class` 与 `style` 之外还有**图标**，
 * 而图标**直接决定按钮宽度**——`.layui-btn .layui-icon{padding:0 2px}`，
 * 一个空 `<i>` 就占 4px、`layui-icon-down`/`layui-icon-more-vertical` 占 16px。
 * 上一版没带图标，实测宽度 96/90/60 vs 对标 100/104/76。
 *
 * 三类图标（`class` 逐字取自对标 DOM，父元素/文案见上表行号）：
 * 1. **真 layui 字形**：`&#xe669;`（刷新）、`layui-icon-more-vertical layui-font-12`（发起/更改签约）、
 *    `layui-icon-down layui-font-12`（检测功能）——这几个有内容，宽度真实来自字形。
 * 2. **对标私有字形类**（`layui-zhuce`/`layui-quanxianguanli1`/`layui-shanchu`）：**对标自己的 CSS 里也没有定义**
 *    （`::before` 的 `content` 实测是 `none`，那些字形在他们页面上同样不显示），所以它们只贡献
 *    那 4px 的 `padding`。**照抄它们不是复刻缺陷，是复刻宽度**：不写就少 4px。
 * 3. 无图标：`禁用/启用`、`设自助接单数`、`设抢单价范围`、`禁/解结算`、
 *    `统计完单率`、`接单统计`、`加入黑名单`。
 *
 * ⚠️ 对标原文写 `<i class="layui-icon layui-biaoji1" />`（自闭合），但**实测对标 DOM 里它是空元素**
 * （`<i class="layui-icon layui-biaoji1"></i>设置标记`，文案是它的**兄弟节点**，实测按钮内文案宽 48px
 * ＝4 字×12px，若被包进 `<i>` 会继承 `.layui-icon{font-family:layui-icon!important;font-size:14px!important}`
 * 变成 56px）。所以我们写显式 `</i>`，拿到与对标相同的 DOM。
 * ⚠️ 2026-09-22：「设置标记」这颗按钮已按业务方裁剪删除，上面这段是**它的实测记录**（保留不改）；
 *    同样的自闭合写法还出现在 `设置游戏权限`/`设置业务类型`/`删除用户` 上，故这条规则仍然适用。
 *
 * `style` **逐字**：只有 `发起/更改签约`（L282，完全无 `style`）与 `禁/解结算`（L293，`style=""`）
 * 两颗拿 CSS 默认值 `.layui-btn-container .layui-btn{margin-right:10px}`，其余 12 颗是内联
 * `margin-right:5px`（实测计算样式：5px vs 10px）。上一版给当时那 21 颗全写了 5px，
 * 于是第 4 颗之后每颗都左移 5px——**探针看不见**（数组项只比对 文案/宽/高，不比对 x）。
 *
 * 不复刻的：对标 `lay-tips="…"`（由他们的 `layuiadmin` 扩展实现，我们 vendored 的
 * `public/layui/layui.js` 里 `grep -c lay-tips` = **0**，对我们完全是死属性）；
 * `lay-event="…"`（**只记录不渲染**，否则 layui 会吞掉冒泡、按钮点了没反应——见 `toolbarHtml()`）；
 * 以及 `发起/更改签约` 上的 `lay-submit="" lay-filter="exportBtn"`——`lay-submit` 在未注册
 * `form.on('submit(exportBtn)')` 时会让表单真的提交（`action=""` 会刷新页面），
 * 那是行为风险而不是视觉值，故只保留 `id`。
 */
type ToolbarButton = {
  /** 对标 `User__userlist.html` 的行号。 */
  line: number
  action: UserToolbarAction
  label: string
  /** `class` 原文（`layui-btn-xs ` 的尾空格也照抄）。 */
  cls: string
  /** `style` 原文；`undefined` = 对标那颗按钮**没有** `style` 属性（不是漏写）。 */
  style?: string
  /** 对标的 `lay-event` 原文（`undefined` = 对标那颗没有）。**只作记录，不写进标记**——原因见
   *  `toolbarHtml()` 里的注释（layui 的表格工具栏会 `return false` 吞掉冒泡，我们的点击就走不到了）。 */
  layEvent?: string
  /** 对标的 `id` 原文（`undefined` = 对标那颗没有）。**14 颗里只有「检测功能」有**
   *  （对标 `User__userlist.html` L296 的 `id="checkDropdown"`）——它是 `dropdown.render`
   *  找落点的唯一凭据（`userlist.js:1280` 的 `elem: '#checkDropdown'`），所以这一颗必须真渲染出 `id`。 */
  id?: string
  /** 按钮内 HTML（图标 + 文案，逐字）。 */
  inner: string
}

const TOOLBAR_BUTTONS: readonly ToolbarButton[] = [
  { line: 279, action: 'new', label: '新增系统子账户', cls: 'layui-btn layui-btn-xs layui-btn-danger', style: 'margin-right:5px', layEvent: 'addUserBtn', inner: '<i class="layui-icon layui-zhuce"></i>新增系统子账户' },
  { line: 280, action: 'refresh', label: '刷新列表', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'refresh', inner: '<i class="layui-icon">&#xe669;</i>刷新列表' },
  { line: 281, action: 'toggle', label: '禁用/启用', cls: 'layui-btn layui-btn-xs layui-btn-warm', style: 'margin-right:5px', layEvent: 'disBtn', inner: '禁用/启用' },
  // L282：无 style 属性（→ margin-right 10px）、无 lay-event、有 id；图标在**文案之后**。
  { line: 282, action: 'notOpen', label: '发起/更改签约', cls: 'layui-btn layui-btn-xs layui-btn-normal', inner: '发起/更改签约<i class="layui-icon layui-icon-more-vertical layui-font-12"></i>' },
  { line: 284, action: 'games', label: '设置游戏权限', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'setGamesBtn', inner: '<i class="layui-icon layui-quanxianguanli1"></i>设置游戏权限' },
  { line: 285, action: 'type', label: '设置业务类型', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'setTypeBtn', inner: '<i class="layui-icon layui-quanxianguanli1"></i>设置业务类型' },
  { line: 286, action: 'selfCount', label: '设自助接单数', cls: 'layui-btn layui-btn-xs ', style: 'margin-right:5px', layEvent: 'setSelfRecOrderCountBtn', inner: '设自助接单数' },
  { line: 287, action: 'price', label: '设抢单价范围', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'setSelfRecOrderPriceBtn', inner: '设抢单价范围' },
  // L293：`style=""`（空值，不是没有属性）→ 拿 CSS 默认的 10px。
  { line: 293, action: 'settle', label: '禁/解结算', cls: 'layui-btn layui-btn-xs', style: '', layEvent: 'setDisSettBtn', inner: '禁/解结算' },
  { line: 294, action: 'notOpen', label: '统计完单率', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'recComplateRate', inner: '统计完单率' },
  { line: 295, action: 'recTotal', label: '接单统计', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'viewRecTotal', inner: '接单统计' },
  // L296：「检测功能」在**同一个** `.layui-btn-container` 里，紧跟「接单统计」之后，箭头在文案后。
  // ⚠️ 14 颗里**只有这一颗**渲染 `id`（对标原文就有 `id="checkDropdown"`）：二级下拉要按它找落点。
  { line: 296, action: 'aliPay', label: '检测功能', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'checkDropdown', id: 'checkDropdown', inner: '检测功能<i class="layui-icon layui-icon-down layui-font-12"></i>' },
  { line: 297, action: 'notOpen', label: '加入黑名单', cls: 'layui-btn layui-btn-xs', style: 'margin-right:5px', layEvent: 'addToBlackBtn', inner: '加入黑名单' },
  // ⭐ Task 8：`action` 从 `notOpen` 改成 `'delete'`（真动作：软删 + 两次确认）。
  // ⚠️ 与 `config/systemUsers.ts` 的 `USER_TOOLBAR` **必须同改**，否则「不漂移」对拍会红。
  { line: 298, action: 'delete', label: '删除用户', cls: 'layui-btn layui-btn-xs layui-btn-warm', style: 'margin-right:5px', layEvent: 'delUserBtn', inner: '<i class="layui-icon layui-shanchu"></i>删除用户' },
]

/** 导出给 spec 做「与 `USER_TOOLBAR` 不漂移」的对拍。 */
export const TOOLBAR_BUTTONS_FOR_TEST: readonly ToolbarButton[] = TOOLBAR_BUTTONS

/**
 * **自有**按钮「押金额度」（对标 `#toolbarDemo` 里**没有**这一颗，2026-10-23 打手个人押金额度线）。
 *
 * 为什么不进 `TOOLBAR_BUTTONS` / `USER_TOOLBAR`：
 * 那两份是「**对标 14 颗**」的逐字副本，`system-user-list.spec.ts` 用「恰好 14 颗 + 与 config 同序
 * 同文案同配色」把两边钉在一起（`AGENTS.md` §5-D「与对标不漂移」）。把自有按钮混进去会让
 * 那条对拍**失去判据力**（它就不再回答「对标那 14 颗漂没漂」）。
 *
 * 所以它单独一份，只在 `toolbarHtml(showDepositQuota = true)` 时**插在「设置游戏权限」之后**
 * （默认 `false` ⇒ 既有测试与无权限用户看到的仍是 14 颗）。
 * 配色/尺寸：`layui-btn-xs` + `layui-btn-warm`（橙）= **刻意与对标那 14 颗的绿/蓝区分**，
 * 业务方 2026-10-23：「把这个押金额度按钮换个颜色凸显出来」。
 * `style` 用 `margin-right:5px`，与它前后那几颗一致（对标 `.layui-btn-container` 的默认 10px
 * 会把这一颗挤出一条缝，见 `TOOLBAR_BUTTONS` 上方的宽度注释）。
 */
export const DEPOSIT_QUOTA_BUTTON = {
  action: 'depositQuota',
  label: '押金额度',
  cls: 'layui-btn layui-btn-xs layui-btn-warm',
  style: 'margin-right:5px',
  inner: '押金额度',
} as const satisfies {
  action: UserToolbarAction
  label: string
  cls: string
  style?: string
  inner: string
}

/** 「押金额度」那颗按钮的标记（形状与 `toolbarHtml()` 里那 14 颗**逐字同构**）。 */
export function depositQuotaButtonHtml(): string {
  const b = DEPOSIT_QUOTA_BUTTON
  const styleAttr = b.style === undefined ? '' : ` style="${b.style}"`
  return `<button class="${b.cls}"${styleAttr} data-tb="${b.action}" data-label="${b.label}" type="button">${b.inner}</button>`
}

/**
 * 业务类型三项的**唯一出处**（顺序 = 业务方裁定的顺序，见 D-0086）。
 * ⚠️ 这段顺序与「新开系统账号」页（`public/replica/user-adduser.html` 的 `bussType` 三个 radio）
 * **必须一致**——同一个字段在两个页面里顺序相反时，用户会以为选错了项。
 * ⚠️ 这里是**展示顺序**，不是 `BusinessType` 的常量顺序：`apps/accounts/choices.py` 仍是 `(0,1,2)`。
 */
export const BUSINESS_TYPE_OPTIONS: readonly { value: number; label: string }[] = [
  { value: 1, label: '专做代练' },
  { value: 2, label: '专做陪玩' },
  { value: 0, label: '代练+陪玩' },
]

/**
 * 「设置业务类型」弹层 `.typebox` 里那三个 **radio** 的标记。
 *
 * ⚠️ **顺序是有意偏离对标原文的一处**：对标 `User__userlist.html` L331-344 的原文顺序是
 * `0 代练+陪玩 / 1 专做代练 / 2 专做陪玩`，这里按业务方 2026-09-22 的决策 **D-0086**
 * （「专做代练排第一」）改成 `1 → 2 → 0`。理由：同一个字段在「新开系统账号」页
 * （`public/replica/user-adduser.html` 的 `bussType`）与列表行内下拉都已按 D-0086 排序，
 * 两个地方顺序相反会让用户以为选错了项。除这一处外，标记与对标原文逐字一致。
 *
 * ⚠️ 三个 radio **都不带 `checked`**：勾选多行且各行业务类型不同时，没有一个值能代表全体，
 * 预选任意一项都会把存量行**静默改值**（E-0027 同型）。改由保存时那句
 * 「请选择业务类型」拦住「没选就保存」（对标 `userlist.js:1626-1630`）。
 */
export function businessTypeRadiosMarkup(): string {
  return BUSINESS_TYPE_OPTIONS.map(
    (o) => `<input type="radio" name="selbussType" value="${o.value}" title="${o.label}" />`,
  ).join('\n          ')
}

/**
 * 四个弹层里那几句用户可见文案，**逐字**取自对标原文（出处逐条标注）。
 * 抽成常量只为让 spec 能钉住字面值（改一个字就红）。
 */
export const DIALOG_MESSAGES = {
  /** `userlist.js:1629`：`input[name=selbussType]:checked` 取不到值时的提示。 */
  businessTypeRequired: '请选择业务类型',
  /** `userlist.js:1757`：标记与原先一样时的提示（对标**不提交**）。 */
  tagUnchanged: '编辑的内容与原先一样，无需提交',
  /** `userlist.js:2336`：排序值不是正整数时的提示。 */
  orderByInvalid: '请输入正整数',
  /**
   * `userlist.js:383`（i18n 字典 `setSelfRecOrderCountmsg`）：接单上限不是正整数时的提示。
   * ⚠️ 与 `orderByInvalid` **字面相同但出处不同**（两处独立文案），所以各留一个键——
   * 合并成一个会让「改了一处的文案」变成两处一起变（那是两个页面各自的措辞，不是同一个）。
   */
  selfCountInvalid: '请输入正整数',
} as const

/**
 * 「删除用户」**两次确认**的逐字文案（Task 8；对标 i18n 字典原文，业务方点名要那句劝退）。
 *
 * ⛔ **如实边界（读这两句时必须连着代码读）**：文案是**照抄对标**的，而**实际行为是软删
 * （可恢复）**——「一旦删除，将彻底无法恢复」/「删除就永远无法恢复了」在**当前实现下并不成立**
 * （`POST /api/accounts/<pk>/delete/` 只写 `deleted_at`，`.../restore/` 能恢复）。
 * 照抄是业务方 2026-09-20 的明确要求（他要的就是那句「建议不删除，只修改有效状态」），
 * 所以**不改文案**；这里显式登记「文案与实现不一致」，别让下一个人以为我们真在做硬删。
 *
 * ⚠️ `<br/>` 与 **`第二次确认:` 后面的两个空格**都是**逐字照抄**（标点/空格都属于文案）。
 */
export const DELETE_CONFIRM_TEXTS = {
  first:
    '确认要删除吗？ <br/> <br/> 警告：一旦删除，将彻底无法恢复，强烈建议不要删除，只需要将此用户的有效状态改为禁用即可，请三思再操作！ <br/>',
  second: '第二次确认:  <br/> <br/> 是不是真的要删除？删除就永远无法恢复了 <br/>',
} as const

/**
 * 「设置业务类型」弹层的实参，**逐字**取自对标 `userlist.js` L1097-1109 的 `layer.open({...})`。
 * `content`（`$('.typebox')`）与 `end`（`$('.typebox').hide()`）在挂载处补——它们要用
 * layui 自带的 jQuery 与页内那个容器。
 * `area` 依赖 `isPc`（对标 `area: [isPc ? '700px' : '80%', '300px']`），所以单独给一个函数。
 */
export const TYPE_DIALOG_BASE = {
  title: '修改业务类型',
  shade: [0.3, '#333'],
  shadeClose: true,
  type: 1,
  skin: 'layui-layer-lan',
  anim: 5,
} as const

export function typeDialogArea(isPc: boolean): [string, string] {
  return [isPc ? '700px' : '80%', '300px']
}

/**
 * 「设抢单价范围」弹层的实参，**逐字**取自对标 `userlist.js` L1136-1148 的 `layer.open({...})`。
 * 同上：`content: $('.recPricebox')` 与 `end: $('.recPricebox').hide()` 在挂载处补。
 */
export const REC_PRICE_DIALOG_BASE = {
  title: '批量设置内部抢单价格范围限制',
  shade: [0.3, '#333'],
  shadeClose: true,
  type: 1,
  skin: 'layui-layer-lan',
  anim: 5,
} as const

export function recPriceDialogArea(isPc: boolean): [string, string] {
  return [isPc ? '410px' : '85%', '230px']
}

/**
 * 「设置游戏权限」的 `layer.open` 实参，**逐字**取自对标 `userlist.js` 里 `setGamesBtn` 的弹层。
 *
 * ⚠️ 这是本页**两个** `type: 2`（iframe 弹层）**之一**：另一个是「新开 / 编辑系统子账户」的
 * `ADD_EDIT_USER_DIALOG_BASE`（对标 `addEditUser`，Task 13 加的）。对标的「设置游戏权限」
 * **不是弹层**，而是一个独立的 iframe 页面 `/User/SetGames?id=<uid>`（我方子页见
 * `pages/system-setgames.ts`）。旧实现把它做成自造的 `<select multiple>` 通用弹层
 * （`openFormDialog`），业务方 2026-09-22 的原话「设置游戏权限和业务类型的弹窗没有复刻成功」
 * 指的就是它——`openFormDialog` 已随本改动删除（它再无调用方）。
 *
 * ⚠️ `title` 是「**设定**游戏权限」（不是工具栏按钮上那句「设置游戏权限」）——对标原文如此。
 * `area` 是 `['80%', '30%']`：它比常规弹层**矮**，所以子页里那两行游戏 tag 会在弹层内部滚动。
 * `end` 回调在挂载处补（要用页内的 `reload()`）：子页保存成功后自己关掉弹层，父页借此刷新列表。
 */
export const SET_GAMES_DIALOG_BASE = {
  title: '设定游戏权限',
  shade: [0.3, '#333'],
  shadeClose: true,
  type: 2,
  skin: 'layui-layer-lan',
  anim: 5,
  area: ['80%', '30%'],
} as const

/**
 * 弹层子页 URL（`/layui-island/page.html`）的**基址**。
 *
 * ⚠️ **与 `API_BASE` 分开是两个常量**：接口走 `VITE_API_BASE`，而**页面是构建产物里的静态资源**，
 * 走 `BASE_URL`。子路径部署（README 的 `TENANT_WEB_BASE=/console/` ⇒ `BASE_URL='/console/'`）下两者不同
 * ——拿 `API_BASE` 拼页 URL 会得到 `/layui-island/page.html`（**少了 `/console` 前缀**）⇒ 弹层**整页 404**。
 * 同仓约定见 `components/LayuiIslandHost.vue`（`${import.meta.env.BASE_URL}layui-island/…`）与
 * `pages/system-roleauthority.ts` 的 `PAGE_BASE`。
 *
 * 写法选**剥尾斜杠再补 `/`**（本页是 `layer.open({type:2})` 的 `content`，与 `system-roleauthority.ts`
 * 同族，照它的写法）：`BASE_URL` 按 Vite 约定总是带尾斜杠（`/` 或 `/console/`），剥掉后拼一次
 * 得到 `/layui-island/…` / `/console/layui-island/…` 两种规范化结果；直接串（`LayuiIslandHost.vue`
 * 那种）依赖「`BASE_URL` 一定带尾斜杠」，这个前提一旦不成立就会拼出 `/consolelayui-island/…`。
 */
const PAGE_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

/**
 * 拼一个 island 子页 URL。`base` **可注入只为测试**能换一个基址验证拼接（默认值就是上面的
 * `PAGE_BASE`，生产调用一律只传第一个实参）——把基址做成入参是为了让「有人又把它写死成绝对路径」
 * 这件事能被测出来（见 `__tests__/system-user-list.spec.ts` 的 `/console/` 那条断言）。
 *
 * ⚠️ 注入的 `base` **也**要按同一规则剥尾斜杠：测试传的是**真实形态**的基址
 * （`BASE_URL` 按 Vite 约定总是带尾斜杠：`/console/`），不剥就会拼出 `/console//layui-island/…`
 * ——那样测的就不是生产那条路径了（`PAGE_BASE` 已剥过一次，默认值进来再剥是 no-op）。
 */
function islandPageUrl(query: string, base: string = PAGE_BASE): string {
  return `${base.replace(/\/$/, '')}/layui-island/page.html?${query}`
}

/**
 * 子页的 island 页 URL（`layer.open({type:2, content})`）。
 * 多个人时对标传的是**逗号串**，子页自己从 `window.location.search` 读 `uid`
 * （island 的 `IslandContext` 不带 query 参数）。
 *
 * ⚠️ `page=` 的值就是子页的**文件名**（`layui-island/page.ts` 用 `import.meta.glob` 按文件名注册），
 * 必须与 `pages/system-setgames.ts` 逐字一致；改文件名要同一次改这里
 * （spec 会钉住 `page=` 的 key 与路径形状（按 `BASE_URL` 拼））。
 */
export function setGamesDialogContent(ids: readonly number[], base: string = PAGE_BASE): string {
  return islandPageUrl(`page=system-setgames&uid=${ids.join(',')}`, base)
}

/**
 * 「押金额度」弹层的 `layer.open` 实参（2026-10-23 打手个人押金额度线；**对标没有这个入口**，
 * 是本页的**自有功能**）。
 *
 * 与同页 `SET_GAMES_DIALOG_BASE` 同形（都是 `type: 2` 的 iframe 弹层、同一套 shade/skin/anim），
 * 只有 `title` 与 `area` 不同：这一页是**表单**（开关 + 每款游戏一行输入），比游戏权限那页高。
 * `content` / `end` 在挂载处补——它们要用页内的 `reload()`（子页保存成功后自己关掉弹层，
 * 父页借此刷新列表）。
 */
export const DEPOSIT_QUOTA_DIALOG_BASE = {
  title: '押金额度',
  shade: [0.3, '#333'],
  shadeClose: true,
  type: 2,
  skin: 'layui-layer-lan',
  anim: 5,
  area: ['720px', '560px'],
} as const

/**
 * 「押金额度」子页的 URL（`page=` 的值必须与 `pages/taker-deposit-quota.ts` 的文件名**逐字一致**，
 * 否则 island 注册表里找不到它、弹层里会是「未登记的 island 页面」）。
 *
 * 只给**一个人**配（计划 §10：「不做批量给多个人配额度」）⇒ `uid` 是**单值**，不是逗号串。
 * 基址走 `PAGE_BASE`（`BASE_URL`），`base` 可注入只为测试（见 `islandPageUrl`）。
 */
export function depositQuotaDialogContent(id: number, base: string = PAGE_BASE): string {
  return islandPageUrl(`page=taker-deposit-quota&uid=${id}`, base)
}

/**
 * 「新开 / 编辑系统子账户」的 `layer.open` 实参，**逐字**取自对标
 * `userlist.js:2191-2202` 的 `addEditUser`（那是本页第二个 `type:2` 弹层，见
 * `SET_GAMES_DIALOG_BASE` 的注释）。
 *
 * ⚠️ `title` **不进**这个常量：对标是 `(id > 0 ? "编辑" : "新开") + "系统子账户"`，随 id 变，
 * 由调用处用 `addEditUserDialogTitle(id)` 补（`end` 同理，要用页内的 `reload()`）。
 */
export const ADD_EDIT_USER_DIALOG_BASE = {
  shade: [0.3, '#333'],
  shadeClose: true,
  type: 2,
  skin: 'layui-layer-lan',
  anim: 5,
  area: ['90%', '90%'],
} as const

/** 对标 `(id > 0 ? "编辑" : "新开") + "系统子账户"`。`id === 0` ⇒ 新增。 */
export function addEditUserDialogTitle(id: number): string {
  return `${id > 0 ? '编辑' : '新开'}系统子账户`
}

/**
 * 子页 URL。对标是 `/User/AddUser?uid=<id>`；我方岛页**认的查询键是 `id=`**
 * （`pages/user-adduser.ts` 的 `resolveFormMode` 第一条分支），键名不同是**有意**的、
 * 不是抄漏——改成 `uid` 会让编辑态静默退化成新增态（对标参数名与我方契约不是一回事）。
 * `id=0` 表示新增（`resolveFormMode` 要求 `id > 0` 才算编辑）。
 *
 * ⚠️ `page=` 的值就是子页的**文件名**；`system-user-list.spec.ts` 把它与
 * `pages/user-adduser.ts` 自己声明的 key、以及真实文件名绑在一起（别手抄两份会漂移的清单）。
 *
 * 基址走上面的 `PAGE_BASE`（`BASE_URL`），`base` 可注入只为测试（见 `islandPageUrl`）。
 */
export function addEditUserDialogContent(id: number, base: string = PAGE_BASE): string {
  return islandPageUrl(`page=user-adduser&id=${id}`, base)
}

/** 「设置标记」`layer.prompt` 的实参（对标 `userlist.js:1755`）；`value` 在挂载处补。 */
export const TAG_PROMPT_BASE = {
  title: '设置用户标记，不需要请填写：无',
  formType: 0,
  maxlength: 100,
} as const

/** 「排序值」`layer.prompt` 的实参（对标 `userlist.js:2330`）；`value` 在挂载处补。 */
export const ORDER_BY_PROMPT_BASE = {
  title: '设置指派接手人时的排序值，值越大越在前，默认0',
  formType: 0,
  maxlength: 4,
} as const

/**
 * 「设自助接单数」`layer.prompt` 的实参，**逐字**取自对标 `userlist.js:2371`
 * （`setSelfRecOrderCount` 的 `layer.prompt({title, formType, value, maxlength})`）。
 *
 * ⚠️ **有意偏离对标的一处**：对标把 `value` 写死成 `0`。照抄的话，用户「不改直接点确定」
 * 会把选中行已有的接单上限**静默改成 0（不限制）**——这正是 E-0027 的同型（一个写入点
 * 改了值、回退/预填路径没跟上，结果没人看得出来）。按 2026-09-22 的裁定改为：
 * **选中行的 `grab_limit_dl` 全部相同时预填该值，否则预填 `0`**（预填函数见
 * `selfCountPromptValue`）。所以 `value` 不在本常量里，由调用处补。
 * ⚠️ `0` 是**合法值**（= 不限制），校验只要求「非负整数」——别把 `0` 当成「没填」。
 */
export const SELF_COUNT_PROMPT_BASE = {
  title: '设置用户在内部接单页面接单上限（以用户当前未完成订单数为准），0代表不限制',
  formType: 0,
  maxlength: 4,
} as const

/**
 * 两张**卡片**的标题（逐字取自对标）：
 * - 「接单统计」＝ `userlist.js:1256`（`viewRecTotal` 的 `title`）；
 * - 「重复收款支付宝子账号检测」＝ `userlist.js:1291`（`checkDropdown` 菜单项里那张卡片的 `title`）。
 *
 * ⚠️ 菜单项自己的文案是**另一句**（「检测重复收款支付宝账号者」），见 `CHECK_DROPDOWN_MENU`。
 * 对标里菜单文案与卡片标题**本来就不一样**，不要「统一」成一句。
 */
export const CARD_TITLES = {
  recTotal: '接单统计',
  aliPay: '重复收款支付宝子账号检测',
} as const

/**
 * 「检测功能」那颗按钮弹出的**二级下拉**的菜单项（`userlist.js:1283-1286`，逐字）：
 * `data: [{ title: '检测重复收款支付宝账号者', id: 10 }]`，`click` 里判 `id == 10`。
 */
export const CHECK_DROPDOWN_MENU: readonly { title: string; id: number }[] = [
  { title: '检测重复收款支付宝账号者', id: 10 },
]

/**
 * 两张卡片**共用**的 `layer.open` 实参（`title` 之外逐字相同）。
 *
 * 出处：`userlist.js:1255-1274`（`viewRecTotal` → 「接单统计」）与
 * `userlist.js:1287-1312`（`checkDropdown` 的 `click` → 「重复收款支付宝子账号检测」）；
 * 两者的 `shade` / `shadeClose` / `type` / `skin` / `anim` / `closeBtn` / `maxmin` / `area`
 * 逐字一致，本函数把它们收成一处，避免两张卡片各写一遍后悄悄漂移。
 *
 * ⚠️ 两处**故意**照抄的行为（不是笔误）：
 * ① 电脑端 `closeBtn: 0` ⇒ **没有右上角关闭按钮**（只能按 Esc / 点遮罩关；非 PC 才给关闭按钮）；
 * ② 非 PC 端在挂载处再补一次 `layer.full(index)`（对标原文，见 `openCard`）。
 * `content`（`$('.recTotalDiv')` / `$('.checkAliPayAccDiv')`）与 `cancel`/`end`（把容器藏回去）
 * 依赖页内容器与 layui 自带的 jQuery，在挂载处补。
 */
export function cardDialogArgs(title: string, isPc: boolean): Record<string, unknown> {
  return {
    title,
    shade: [0.3, '#333'],
    shadeClose: isPc ? true : false,
    type: 1,
    skin: 'layui-layer-lan',
    anim: 8,
    closeBtn: isPc ? 0 : 1,
    maxmin: isPc ? false : true,
    area: [isPc ? '38%' : '95%', isPc ? '90%' : '95%'],
  }
}

/**
 * 对标 `assets_setgames/JS2019/common.js:2-8` 的 `VerifyMoeny`——**正则逐字照抄**：
 * 允许 `+`/`-` 号、允许 1–2 位小数；**不允许**千分位、科学计数法、空串、纯小数点。
 * ⚠️ 它**不要求整数**（尽管提示文案写的是「必须是…之间的整数」）——照抄，不要「修正」。
 */
function isMoneyLike(value: string): boolean {
  return /^(((\-|\+)?[1-9]\d*)|0)(\.\d{1,2})?$/.test(value)
}

/**
 * 「排序值」的正整数判据。
 *
 * ⚠️ **这一处没有照抄对标的 `VerifyInt`（`common.js:18-24`，`/^\+?[0-9]*$/`）**，理由是它有两个
 * 会**造成实际后果**的洞（对标缺陷不复刻，同 `config/systemUsers.ts` 开头那条口径）：
 * ① 它**接受空串** ⇒ 用户清空输入框点确定，`Number('') === 0` 会把排序值**静默写成 0**；
 * ② 它**接受纯 `+`** ⇒ `Number('+') === NaN`，JSON 里变成 `null`，后端 `IntegerField` 判非法。
 * `/^\d+$/` 才是文案说的「正整数」（至少一位数字、只有数字）。提示文案**逐字**照旧
 * （`DIALOG_MESSAGES.orderByInvalid`，对标 `userlist.js:2336`）。
 */
export function isPositiveInt(value: string): boolean {
  return /^\d+$/.test(value)
}

/**
 * 「设自助接单数」prompt 的**预填值**（有意偏离对标的一处，理由见 `SELF_COUNT_PROMPT_BASE`）。
 *
 * 规则（2026-09-22 裁定）：选中行的接单上限**全部相同**时预填该值，否则预填 `0`。
 * - 「键缺失」（`auth.User` 没有档案行 ⇒ `_profile_fields` 整个不给 `grab_limit_dl`）按
 *   **模型默认 `0`** 归一（`StaffProfile.grab_limit_dl` 是 `default=0`）——`?? 0` 不是「兜底好看」，
 *   它就是那个字段在库里的取值；不归一会把 `undefined` 塞进输入框（渲染成空串）。
 * - 全部缺失 ⇒ 每行都是 `0` ⇒ 预填 `0`（「相同」成立且值就是 0）。
 * - ⚠️ 混合取值仍预填 `0`，用户不改直接点确定就会把上限都改成 0 —— 这是**已知残余风险**，
 *   与对标的写死 `value: 0` 相比至少「0 会出现在输入框里」，用户看得见自己按下去的是什么；
 *   彻底消除它需要逐行弹窗，超出本任务（也是对标没做的事）。
 */
export function selfCountPromptValue(rows: readonly { grab_limit_dl?: number }[]): number {
  const limits = rows.map((r) => r.grab_limit_dl ?? 0)
  const first = limits[0] ?? 0
  return limits.every((v) => v === first) ? first : 0
}

/**
 * 「设抢单价范围」保存时的 **6 条校验**：文案与顺序**逐字**照抄对标 `userlist.js:1661-1687`。
 * 返回**第一条**不通过的提示文案；全部通过返回 `null`。
 *
 * ⚠️ 前两条（空值）在页面上已被容器里的默认值 `0` / `99999` 挡住——照抄，不要「优化掉」。
 * ⚠️ 只有这 6 条：**没有**第 7 条，也**没有**「上限 ≥ 下限」（对标前端没有，那条由后端与
 * 模型 CHECK 负责）。
 * ⚠️ 第 5/6 条在对标里是字符串与数字直接比较（`price_min < 0`）；这里写 `Number(...)`，
 * 语义相同——能走到这两条的值已通过 `isMoneyLike`，一定是数字串。
 */
export function recPriceValidationMessage(priceMin: string, priceMax: string): string | null {
  if (priceMin === '') return '请输入最小接单价，不能小于0'
  if (priceMax === '') return '请输入最大接单价，不能大于99999'
  if (!isMoneyLike(priceMin)) return '最小接单价格式不正确，必须是0到99999之间的整数'
  if (!isMoneyLike(priceMax)) return '最大接单价格式不正确，必须是0到99999之间的整数'
  if (Number(priceMin) < 0) return '最小接单价必须大于或等于0'
  if (Number(priceMax) > 99999) return '最大接单价必须小于或等于99999'
  return null
}

export function toolbarHtml(showDepositQuota = false): string {
  const buttons = TOOLBAR_BUTTONS.map((b) => {
    const styleAttr = b.style === undefined ? '' : ` style="${b.style}"`
    // ⚠️ 14 颗里只有「检测功能」有 `id`（`TOOLBAR_BUTTONS` 里只有它带 `id` 字段）：
    // 二级下拉 `dropdown.render({elem:'#checkDropdown'})` 按它找落点。其余 13 颗**不许**有 id。
    const idAttr = b.id === undefined ? '' : ` id="${b.id}"`
    // ⚠️ **故意不写 `lay-event`**（`b.layEvent` 只作记录）。
    // 踩过的坑：照抄对标的 `lay-event` 之后，工具栏按钮**点了不再提示「尚未开放」**。
    // 根因：layui 的表格模块对 `.layui-table-tool` 里的 `*[lay-event]` 做了委托点击，处理完
    // **`return false`**（jQuery 的 `return false` = `preventDefault` + `stopPropagation`），
    // 事件到不了我们挂在 `root` 上的 `data-tb` 委托。**实测 A/B**（同一页面，只差这个属性）：
    // 带 `lay-event` 点击后 `.layui-layer` 数 = 0；`removeAttribute('lay-event')` 后再点 = 1，
    // 文案「「统计完单率」尚未开放」。对标之所以能用 `lay-event`，是因为它在 `table.on('toolbar(...)')`
    // 里接住了事件；我们走自己的委托，两者只能选一个。
    return `<button class="${b.cls}"${idAttr}${styleAttr} data-tb="${b.action}" data-label="${b.label}" type="button">${b.inner}</button>`
  })
  // ⭐ 自有按钮「押金额度」插在「设置游戏权限」**之后**（计划 §7.1）；默认不画 ⇒ 仍是 14 颗。
  // 位置按 `action === 'games'` 找，不写死下标：`TOOLBAR_BUTTONS` 会被业务方继续裁剪，
  // 下标一挪，按钮就会跑到别的位置（E-0011 同型：别把「数组位置」当契约）。
  if (showDepositQuota) {
    const gamesIndex = TOOLBAR_BUTTONS.findIndex((b) => b.action === 'games')
    buttons.splice(gamesIndex + 1, 0, depositQuotaButtonHtml())
  }
  // ⚠️ 对标 `#toolbarDemo` 的原文里，按钮容器**后面**紧跟这行统计文案（`<div style="color:#ff6a00" id="total">`）。
  // 工具栏被 layui 渲染在表格视图的**顶部**，所以这行字出现在「按钮下方、表格上方」——
  // 我上一版把它挂在表格后面，位置就错了（业务方实测指出）。实测对标 DOM：两者的父节点都是
  // `.layui-table-tool-temp`。
  return `<div class="layui-btn-container">${buttons.join('')}</div><div style="color:#ff6a00" id="total"></div>`
}



type Col = Record<string, unknown>

/**
 * ⭐ **自有列**「个人押金额度」的标题 / `field` / `lay-filter`（2026-10-23 追加）。
 *
 * 这是本页**第二处**「对标没有、我方自有」的可见物（第一处是工具栏那颗「押金额度」按钮，
 * 见 `DEPOSIT_QUOTA_BUTTON`）。两处**同生同灭**：都只在持 `finance.taker_wallet_adjust`
 * 节点时渲染（`buildCols(..., showDepositQuota = true)` / `toolbarHtml(true)`）。
 *
 * ## 这一列是什么（业务口径）
 *
 * 「允许这个人按游戏配个人押金额度」的**总闸**——⛔ **不是**「免押金」本身。
 * 关着 ⇒ 这个人走全局按游戏押金、弹层只读；开着 ⇒ 弹层里的按游戏个人额度才生效
 * （某款游戏填 0 才是「该游戏免押」）。
 *
 * ## 为什么不做成「行内编辑现有列」而要加一列
 *
 * 对标那 22 个有标题的列里没有这个状态（对标压根没有「个人押金额度」这套东西），
 * 也没有合适的列可以搭车：`保证金` 是 `StaffProfile.deposit_amount`（记账，不参与接单判断），
 * `有效状态` / `禁止结算` / `禁自助接单` 各有各的语义。塞进任何一列都会让那一列说两件事。
 *
 * ## 代价（如实登记）
 *
 * ① 表宽**再涨一列**：本页的定宽列之和已到 1649px（视口 1650），加一列必然出现横向滚动条
 *    （差异 #7 的同一条根因，见 `buildCols` 里那五列「不许补 width」的注释）；
 * ② 这列**只有持节点的人看得到** ⇒ 同一张表对不同角色宽度不同（＝有意为之，fail-closed，
 *    D-0154 / E-0098：不能让用户看到一个点了必 403 的入口）。
 */
export const DEPOSIT_QUOTA_COLUMN = {
  field: 'deposit_quota_enabled',
  title: '个人押金额度',
  width: 110,
  filter: 'depositQuotaEnabled',
  /** 开关文案：开 / 关（与弹层里那颗 switch 的「启用|关闭」不是同一处控件，措辞各自独立）。 */
  text: '开|关',
} as const

/**
 * 主表列定义（对标 `userlist.js:654-801`）。
 * 可见 20 列 / `hide:true` 6 列 —— 与对标截图的默认可见列一致。
 *
 * ⭐ `showDepositQuota = true` 时**额外**插一列自有列「个人押金额度」
 * （位置：紧跟「保证金」之后——它是押金口径的开关，放在金额旁边最不容易读错；
 * 默认 `false` ⇒ 无节点的人与既有断言看到的仍是那 22 列，fail-closed）。
 */
export function buildCols(gameNames: Map<number, string>, showDepositQuota = false): Col[] {
  const switchCell = (checked: boolean, text: string, filter: string, id?: number): string =>
    `<input type="checkbox" lay-skin="switch" lay-text="${text}" ${checked ? 'checked' : ''} lay-filter="${filter}"${id === undefined ? '' : ` data-id="${id}"`}>`

  /** 对标 `userlist.js` 的「业类」徽标：`0`→蓝「代陪」、`2`→橙「陪玩」、`1`→绿「代练」。 */
  const bussBadge = (value: number | undefined): string => {
    if (value === 0) return '<span class="layui-badge layui-bg-blue">代陪</span>'
    if (value === 2) return '<span class="layui-badge layui-bg-orange">陪玩</span>'
    if (value === 1) return '<span class="layui-badge layui-bg-green">代练</span>'
    return ''
  }

  /** 对标：账号链接**有效为蓝 `#1E9FFF`、禁用为红 `#FF534D`**。 */
  const accountLink = (d: AccountRow, text: string): string =>
    `<a class="user-cell-act" data-act="edit" data-id="${d.id}" style="color:${d.is_active ? '#1E9FFF' : '#FF534D'}">${text}</a>`

  /**
   * 开关列的**显式有序拼装**（⛔ **不要**再改回 `slice(0, n)` / `slice(n)` 位置法）。
   *
   * 为什么必须显式：位置法把「列顺序」绑在 `UNSUPPORTED_USER_SWITCHES` 的**条目顺序**上——
   * 2026-09-23 把「禁止结算」从那份名单里移出、接成真开关时，那条 `slice(0, 4)` 立刻错位
   * （会少放一列未开放开关，并把真开关挤到「禁看内容区」后面，与对标不符）。
   *
   * ⭐ **2026-09-23 合并 `main`**：`main` 上另一条线独立接了同样两个开关、并**保留**那 4 个
   * 「后端没有 ⇒ 点了提示尚未开放」的开关列（`登录免验证` / `隐藏订单电话` / `禁看密码` /
   * `禁看内容区`）。本线已按**业务方 2026-09-22 的裁定**把那 4 列**删除**（D-0096 的裁剪清单）
   * ⇒ 合并后这里只列**真开关**；`UNSUPPORTED_USER_SWITCHES` / `notOpenSwitchCol` /
   * `unsupportedSwitch` 三个符号**整份不存在**（照搬 `main` 那段会直接抛错）。
   *
   * 顺序**逐条**取自对标 `userlist.js:753-776`（2026-09-23 当场 grep，行号是移动靶）：
   *   `sta`(754, 有效状态) → `isProhibitSettStatus`(763, 禁止结算)
   *   → `isProhibitPublicRecOrder`(772, 禁自助接单)；
   * ⚠️ 中间那 4 个未开放列按裁定删除，故这里不列。
   */
  const switchCols: Col[] = [
    {
      field: 'is_active',
      title: '有效状态',
      width: 75,
      align: 'center',
      templet: (d: AccountRow) => switchCell(d.is_active, '正常|禁用', 'staChecked', d.id),
    },
    // 两个真开关列（`REAL_SWITCHES` 是它们的**唯一出处**，含 field/label/width/filter）。
    // ⚠️ Task 7 起**真读真写**：`checked` 取该行真实布尔值（`=== true` 归一——`false` 与
    // 「键缺失」都不勾，E-0021 的边界），`lay-filter` 用 `REAL_SWITCHES` 配的那个名字
    // （处理器在挂载处按它注册），`data-id` 给处理器的 PATCH 指认是哪一行。
    ...REAL_SWITCHES.map((c) => ({
      field: c.field,
      title: c.label,
      width: c.width,
      align: 'center',
      templet: (d: AccountRow) => switchCell(d[c.field] === true, '是|否', c.filter, d.id),
    })),
  ]
  const cols: Col[] = [
    // ⚠️ 宽度/固定/隐藏/对齐**逐条照抄** `userlist.js:654-801`（**实测**）。
    // 上一版我把宽度抄大了（登录账号 130 vs 90–110、业类 80 vs 50、操作 140+fixed-right vs 88 不固定），
    // 表看着就「大了一圈」。
    //
    // ⚠️⚠️ **五列刻意不写 `width`/`minWidth`**（登录账号、账号昵称、备注说明、标记、游戏权限）：
    // 对标这五列写的是 **`minwidth`（小写 w）**，而 layui 只认 `minWidth`——所以对标那五个
    // `minwidth: 90/110/120/82/95` **全部无效**，它们在 layui 眼里是「自动列」，
    // 由 layui 把「表宽 − 定宽列之和」**均分**：实测 1649−1167=482 → 96/96/96/96/98。
    // 上一版把这五列「修好」成了合法的 `minWidth`/`width`，于是自动列被撑到 91/111/121/83/96，
    // 总宽 1669 **超过视口 1650** → 出现横向滚动条（差异 #7 的根因）。
    // 我们**不写那个拼错的属性**（那不是复刻缺陷），只复刻它的**渲染结果**：让它们保持自动列。
    // 证据：`.tools/shots/probe-bench.json` 的 `ths` w 值 + `table box.w=1649`；
    // 对标 cols 原文 `userlist.js` L658/L664/L748/L778/L784。
    { type: 'checkbox', fixed: 'left', width: 30 },
    {
      field: 'username',
      title: '登录账号',
      // 对标 `minwidth: 90`（无效）＋ `maxWidth: 110`（有效）。`maxWidth` 要留。
      maxWidth: 110,
      fixed: 'left',
      templet: (d: AccountRow) => accountLink(d, d.username),
    },
    {
      field: 'display_name',
      title: '账号昵称',
      // 对标 `minwidth: 110`（无效）＋ `maxWidth: 110`（有效）。
      maxWidth: 110,
      // 对标 `userName` 的 templet 里那个「内/外」徽标**是死代码**（`userTypeSpan` 拼好了却没拼进 `vo`），
      // 实际渲染的只有账号链接 —— 业务方也指出「昵称是文字，不该是方块里塞字」。
      templet: (d: AccountRow) => accountLink(d, d.display_name),
    },
    { field: 'rname', title: '姓名', width: 90, hide: true, templet: () => '' },
    {
      field: 'bussType',
      title: '业类',
      width: 50,
      align: 'center',
      templet: (d: AccountRow) => bussBadge(d.business_type),
    },
    { field: 'signStatus', title: '签约', width: 40, hide: true, align: 'center', templet: () => '' },
    {
      // 对标 `jobName` 的 templet：`<span class="lableb" style="background-color:#9ACD32">职位</span>`
      // —— `.lableb` 是 zdy.css 里的小徽标（见 ISLAND_OVERRIDES 的取值）。
      field: 'role',
      title: '角色职位',
      width: 83,
      templet: (d: AccountRow) =>
        d.role
          ? `<span class="lableb" style="background-color:#9ACD32" title="${d.role}">${d.role}</span>`
          : '',
    },
    { field: 'deposit_amount', title: '保证金', width: 50, edit: 'text', align: 'center' },
    // ⭐ 自有列（对标没有）：个人押金额度总闸。只有持节点的人那一次才插进来
    // （`showDepositQuota`，见 `DEPOSIT_QUOTA_COLUMN` 的注释与 `buildCols` 的签名）。
    // 开关的初值取 `=== true`（键缺失 / false 都不勾，E-0021 的边界）；
    // `data-id` 是切换时指认哪一行的唯一凭据。
    ...(showDepositQuota
      ? [
          {
            field: DEPOSIT_QUOTA_COLUMN.field,
            title: DEPOSIT_QUOTA_COLUMN.title,
            width: DEPOSIT_QUOTA_COLUMN.width,
            align: 'center',
            templet: (d: AccountRow) =>
              switchCell(
                d.deposit_quota_enabled === true,
                DEPOSIT_QUOTA_COLUMN.text,
                DEPOSIT_QUOTA_COLUMN.filter,
                d.id,
              ),
          },
        ]
      : []),
    {
      field: 'grab_limit_dl',
      title: '接单上限',
      width: 60,
      align: 'center',
      templet: (d: AccountRow) => `${d.grab_limit_dl ?? ''}`,
    },
    { field: 'complateRate', title: '完单率', width: 65, hide: true, align: 'center', templet: () => '' },
    {
      field: 'priceLimit',
      title: '接单价限制',
      width: 80,
      hide: true,
      align: 'center',
      // 对标 `userlist.js:731` 的分隔符是**半角连字符** `'-'`（不是 `~`），且**没有空格**。
      templet: (d: AccountRow) => `${d.grab_price_min ?? ''}-${d.grab_price_max ?? ''}`,
    },
    { field: 'qq', title: 'QQ/微信', width: 90, templet: (d: AccountRow) => d.qq || '' },
    { field: 'mobile', title: '电话', width: 90, align: 'left', templet: (d: AccountRow) => d.mobile || '' },
    {
      field: 'remark',
      title: '备注说明',
      // 对标 `minwidth: 120`（无效）→ 自动列。**不要**补 `width`/`minWidth`（见上面的长注释）。
      templet: (d: AccountRow) => d.remark || '',
    },
    // 开关列：**按对标顺序**（`userlist.js:753-776`，2026-09-23 当场 grep）——顺序与理由
    // 逐条写在 `switchCols` 的注释里；这里只做展开，⛔ 不再用 `slice` 下标拼位置。
    ...switchCols,
    {
      field: 'tag',
      title: '标记',
      // 对标 `minwidth: 82`（无效）→ 自动列。**不要**补 `width`/`minWidth`。
      templet: (d: AccountRow) =>
        `<a class="user-cell-act" data-act="tag" data-id="${d.id}">${d.tag || '无'}</a>`,
    },
    {
      field: 'games',
      title: '游戏权限',
      // 对标 `minwidth: 95`（无效）→ 自动列。**不要**补 `width`/`minWidth`。
      templet: (d: AccountRow) => {
        const names = d.games.map((id) => gameNames.get(id) ?? id)
        return `<a class="user-cell-act" data-act="games" data-id="${d.id}">${names.length > 0 ? names.join(',') : '无'}</a>`
      },
    },
    {
      field: 'lastLoginTime',
      title: '最后登录',
      width: 90,
      hide: true,
      align: 'center',
      templet: (d: AccountRow) => d.last_login ?? '',
    },
    {
      field: 'orderBy',
      title: '排序值',
      width: 50,
      align: 'center',
      // 对标 `userlist.js:793-796`：
      // `<a href="javascript:void(0)" class="editOrderBy" data-id="uid" data-orderby="orderBy"
      //   title="在指派界面按排序值排列时，数值越大越排前面，点击可以编辑">orderBy</a>`。
      // 我方 `order_by` 来自 `StaffProfile.order_by`（后端 `default=0`，见 `api/accounts.ts` 的
      // `AccountProfile.order_by`），`?? 0` 保证 **`0` 不被渲染成空**（`0` 是合法且常见的值，
      // 用真值判断会把它吃掉——E-0021）。
      // ⚠️ 只加 `data-act`（我们的委托入口）：**当前值就是链接文案**，所以不再另写 `data-orderby`。
      templet: (d: AccountRow) =>
        `<a class="editOrderBy" data-act="orderby" data-id="${d.id}" title="在指派界面按排序值排列时，数值越大越排前面，点击可以编辑">${d.order_by ?? 0}</a>`,
    },
    { field: 'createUserName', title: '开户人', width: 75, hide: true, align: 'center', templet: () => '' },
    // 对标 `userlist.js:801`：**`fixed: ''`（不固定）**、宽 88、`toolbar: '#baroperating'`。
    // 对标那颗「重置」是 **`layui-btn-danger`（红）**，不是橙色；两颗按钮都在。
    // ⚠️ 两颗 `<a>` 之间**保留了换行**：对标 `#baroperating` 模板（`User__userlist.html` L300-301）
    // 里它们是两行，折叠成一个空格 → 实测对标 `firstRowTds[26]` 文案是「编辑 重置」（**带空格**），
    // 我们上一版写成无空格拼接，探针报「编辑重置」。这里用 `\n` 复刻那个空格。
    {
      title: '操作',
      width: 88,
      // ⭐ Task 8：**已删的行只给「恢复」**（`deleted_at` 有值）——编辑/重置对它没有意义
      // （它不在默认列表里，能出现在这一行的唯一路径就是「有效状态」筛了「已删除」）。
      // ⚠️ 对标**没有**回收站 ⇒ 这颗链接是我方新增（没有原文可抄），注释在此标明。
      templet: (d: AccountRow) =>
        d.deleted_at
          ? `<a class="layui-btn layui-btn-xs layui-btn-normal" style="margin-right:0px" title="恢复该用户" data-act="restore" data-id="${d.id}">恢复</a>`
          : `<a class="layui-btn layui-btn-xs" style="margin-right:0px" title="编辑用户资料" data-act="edit" data-id="${d.id}">编辑</a>\n` +
            `<a class="layui-btn layui-btn-xs layui-btn-danger" style="margin-left:2px" title="重置该用户的登录密码为123456" data-act="reset" data-id="${d.id}">重置</a>`,
    },
  ]
  return cols
}

/**
 * 拼 `<option>`。**每个 option 之间留换行**：对标每个 `<option>` 各占一行
 * （`User__userlist.html` L117-134），而 layui 会把原 `<select>` 藏起来再渲染一份克隆，
 * 于是探针取到的 `.layui-input-inline` 文本是 `innerText || textContent`——两者都是
 * **隐藏 select 里 option 的 textContent 拼接**。不留换行，就会读成 `全部有效禁用…`；
 * 留了换行，才与对标读成同一串 `全部 有效 禁用 …`（不影响任何可见渲染）。
 *
 * `selectedValue`：对标有几处 `<option … selected="selected">`（L149 `status`、L174 `userType`、
 * L181 `isCert`、L188 `isDisSett`、L202 组群状态），这个属性**不是装饰**——`formselects-v4`
 * 靠 `option[selected]`（属性，不是 `selected` 属性值）决定初始选中项，没有它多选标签区是空的。
 * （L202 那个「组群状态」筛选项已按业务方 2026-09-22 的裁剪删除，本页不再渲染它。）
 * 普通 `<select>` 由 layui 渲染时会自动选第一项，加不加看不出差别。
 * ⚠️ `bussType` **也要给** `selectedValue = -1`（＝它的「全部」哨兵）：对标原文那里没有
 * `selected`，但那是**对标把「全部」的取值写在 `0` 上的时代**——插件的初始选中项只认
 * `option[selected]`（`public/vendor/formselects-v4.js:264`），不给就一项都不选、占位文案还会
 * 从「全部」退化成「请选择」（同文件 `:168`）。给了才与同页 `userType`（「全部」= `-1`）一致。
 */
const selectOptions = (
  options: readonly { label: string; value: string | number }[],
  selectedValue?: string | number,
): string =>
  options
    .map(
      (o) =>
        `<option value="${o.value}"${
          selectedValue !== undefined && String(o.value) === String(selectedValue) ? ' selected="selected"' : ''
        }>${o.label}</option>`,
    )
    .join('\n')

/**
 * 三个多选下拉的 **formselects-v4（xm-select）属性**，逐字取自对标 `User__userlist.html`
 * （行号见下），换行/缩进被归一成单个空格——属性顺序与值都照抄，渲染参数由插件从这些属性读：
 *
 * | 键 | 对标行号 | 原文属性 |
 * |---|---|---|
 * | `jobRole` | L142-143 | `xm-select="jobRole" xm-select-skin="default" xm-select-height="36px"` |
 * | `bussType` | L160-161 | `xm-select="bussType" xm-select-radio xm-select-skin="default" xm-select-height="36px"` |
 * | `userType` | L171-172 | `xm-select="userType" xm-select-radio xm-select-skin="default" xm-select-height="36px"` |
 *
 * 关键差异：**`选择职位` 没有 `xm-select-radio`**（多选，下拉里才有「全选/清空/反选」提示条），
 * 而 `业务类型`/`成员类型` 有（单选）。少抄一个属性就会让下拉形状与对标不同。
 *
 * 对标 `jobRole` 的 `<option>` 是**空的**，角色由 `userlist.js` 调
 * `/SystemAjax/GetAllRoleList` 后 `formSelects.data('jobRole','local',{arr:…})` 动态灌入；
 * 我们的角色来自自家 `fetchAccountRoles()`，直接写成静态 `<option>`，插件初始化时读同一份即可。
 */
const XM_SELECT_ATTRS = {
  jobRole: ' xm-select="jobRole" xm-select-skin="default" xm-select-height="36px"',
  bussType: ' xm-select="bussType" xm-select-radio xm-select-skin="default" xm-select-height="36px"',
  userType: ' xm-select="userType" xm-select-radio xm-select-skin="default" xm-select-height="36px"',
} as const

/**
 * 筛选栏。**逐条照抄对标的标记**（实测）：
 * `<form class="layui-form"> → <div class="layui-form-pane"> → 每个 .layui-inline 里
 * label 内联 `width:80px` + `.layui-input-inline` 内联各自宽度`；
 * 搜索按钮是 **`layui-btn-normal`（蓝）且 `width:80px`**，而且对标**没有「重置」按钮**。
 */
function filtersHtml(roles: string[], games: ConfigRow[]): string {
  // option 之间留换行，理由见 `selectOptions` 上方的注释。
  const gameOptions = games.map((g) => `<option value="${g.id}">${g.name}</option>`).join('\n')
  const roleOptions = roles.map((r) => `<option value="${r}">${r}</option>`).join('\n')
  const field = (label: string, width: number, inner: string): string =>
    // ⚠️ `${inner}` 两侧的换行**不能省**：对标在 `<select>` 前后各有一个空白文本节点
    // （`User__userlist.html` L148-156 的缩进），layui 渲染下拉克隆时会把克隆 div 插在
    // `<select>` 后面、把那个空白节点挤到克隆之后，于是探针读到
    // `.layui-input-inline` 的 `textContent` 是「全部 有效 禁用 全部有效禁用」——
    // **两段之间那个空格就是它**。我们上一版写成一行，读出来是「全部 有效 禁用全部有效禁用」。
    // 空白节点本身不影响任何可见渲染（`.layui-form-select` 是绝对定位、原 select 被隐藏）。
    `<div class="layui-inline"><label class="layui-form-label" style="width:80px">${label}</label><div class="layui-input-inline" style="width:${width}px">\n${inner}\n</div></div>`
  // `<select>` **内容两侧**同样要有换行（对标 L148-152：`<select>` 与第一个 `<option>` 不同行），
  // 否则 select 的 textContent 会以「禁用」结尾，那个空格就没了（见 `field` 的注释）。
  const sel = (name: string, opts: string, attrs = ''): string =>
    `<select name="${name}"${attrs}>\n${opts}\n</select>`
  return `
    <div style="padding-left:5px;padding-top:10px">
      <div class="layui-form-pane">
        ${field('游戏权限', 140, sel('gameName', `<option value="">全部</option>\n${gameOptions}`))}
        ${field('选择职位', 130, sel('jobRole', `<option value="">请选择</option>\n${roleOptions}`, XM_SELECT_ATTRS.jobRole))}
        ${field('有效状态', 100, sel('status', selectOptions(USER_STATUS_FILTER_OPTIONS, '')))}
        ${field('业务类型', 100, sel('bussType', selectOptions(BUSINESS_TYPE_FILTER_OPTIONS, -1), XM_SELECT_ATTRS.bussType))}
        ${field('成员类型', 100, sel('userType', selectOptions(MEMBER_TYPE_FILTER_OPTIONS, -1), XM_SELECT_ATTRS.userType))}
        ${field('是否实名', 100, sel('isCert', selectOptions(CERT_FILTER_OPTIONS, '')))}
        ${field('禁止结算', 100, sel('isDisSett', selectOptions(SETTLE_FILTER_OPTIONS, '')))}
        ${field('搜索1', 140, '<input type="text" name="key" placeholder="账号/姓名/电话/QQ微信" class="layui-input">')}
        ${field('搜索2', 130, '<input type="text" name="key2" placeholder="支付宝账号/姓名" class="layui-input">')}
        ${field('搜索3', 140, '<input type="text" name="key3" placeholder="备注/标记" class="layui-input">')}
        <div class="layui-inline">
          <div class="layui-input-inline" style="">
            <button class="layui-btn layui-btn-normal" lay-submit lay-filter="seachBtn" type="button" style="width:80px">搜索</button>
          </div>
        </div>
      </div>
    </div>`
}

/**
 * 筛选栏 → `/api/accounts/` 的查询参数（Task 3：把 7 个筛选做成真筛选）。
 *
 * 从 `form.val('sform')` 取到的值里挑出 10 个筛选控件、落到后端参数名上：
 *
 * | 控件 `name` | 后端参数 | 取值规则 |
 * |---|---|---|
 * | `jobRole` | `role` | 空 = 全部；**多选** ⇒ 原样发逗号串（后端按逗号切分） |
 * | `status` | `is_active` | 只有 `'true'`/`'false'` 发送 |
 * | `key` | `key` | 空 = 全部 |
 * | `gameName` | `game`（按 **id**） | 空串 = 全部 |
 * | `bussType` | `business_type` | **`'-1'` = 全部（不发送）**；`'0'` 是合法值，**必须发送** |
 * | `userType` | `member_type` | `'-1'` = 全部（**不发送**） |
 * | `isCert` | `is_cert`（`Y`/`N`） | 空 = 全部 |
 * | `isDisSett` | `settle_forbidden`（布尔） | 空 = 全部 |
 * | `key2` | `key2` | 空 = 全部 |
 * | `key3` | `key3` | 空 = 全部 |
 *
 * ⚠️ 「全部」哨兵**有两处是 `-1`**（`bussType` 与 `userType`）：`bussType` 的哨兵先是 `0`、
 * 后改空串、最终定为 `-1` —— 每一次都是被实测推翻的，理由与原始输出见
 * `config/systemUsers.ts` 的 `BUSINESS_TYPE_FILTER_OPTIONS`（空串会被 `formselects-v4` 跳过）
 * 与 `task-4-report.md` 的「改动 0」。
 * ⚠️ **`bussType` 的判据是「非空且不是 `-1`」**：`'0'` 是**合法且必须发送**的值，真值判断对
 * `'0'` 为真、对空串 / `undefined` 为假（这正是 `E-0021` 要的形状，别写回 `!== '0'`）；
 * `-1` 则要用**显式**比较挡掉——它是真值，光靠真值判断会把它当合法值发出去。
 * ⚠️ `settle_forbidden` 是**布尔**：`'N'` → `false`，而 `false` 是**合法取值**
 * （只看未禁止结算的人），不能当成「没传」丢掉（E-0021）。
 *
 * 三个 `xm-select` 下拉（`jobRole`/`bussType`/`userType`）的值**能**从 `form.val` 读到：
 * formselects-v4 会把原 `<select>` 的 `name` 摘掉（改存 `_name`）、另插一个
 * `input.xm-hide-input[name=<原 name>]` 并随选中项同步它的 `value`
 * （`public/vendor/formselects-v4.js:309-310` 摘 name、`:1117` / `:1152-1159` 同步）。
 * 所以 `form.val('sform')` 读到的是那个 hidden input 的值，与插件 `value(id,'valStr')` 一致
 * ——实测见 `task-3-report.md` 的「formselects-v4 实测」一节，**别改成读插件 API**。
 */
export function buildUserListWhere(values: Record<string, string>): Record<string, unknown> {
  const where: Record<string, unknown> = {}
  // 「选择职位」是**多选**：`values.jobRole` 是 formselects 拼好的逗号串（`"客服,财务"`），
  // **原样透传**给后端 `role`（后端 `MultiRoleField` 按逗号切分并逐个校验）。
  // ⛔ 不要在这里 split / 取第一个 / 截断——那会丢掉一半职位，且打破前后端对拍用例。
  if (values.jobRole) where.role = values.jobRole
  // 「有效状态」这一个下拉现在有 **4** 个取值（Task 8 加了「已删除」），所以要**分支**：
  //   `'deleted'`（**字符串**，不是布尔）⇒ `deleted=true`（只看已删的人，回收站口径）；
  //   `'true'` / `'false'` ⇒ 仍然发 `is_active`（**旧的语义没被抢走**）；
  //   `''`（全部）⇒ 两个都不发。
  // ⚠️ 别把 `'deleted'` 塞进 `is_active`——后端 `BooleanField` 会把它拒成 1000。
  if (values.status === 'deleted') {
    where.deleted = true
  } else if (values.status === 'true' || values.status === 'false') {
    where.is_active = values.status
  }
  if (values.key) where.key = values.key
  // 游戏权限：按 id 取值；空串 = 全部
  if (values.gameName) where.game = values.gameName
  // 业务类型：**非空且不是哨兵 `-1` 就发送**——`'0'`（代练+陪玩）是**合法且必须发送**的值。
  // 真值判断对 `'0'` 为真、对空串 / `undefined` 为假（E-0021 的形状：⛔ 别写回 `!== '0'`），
  // 但 `-1` 是**真值**，必须显式挡掉（`if (values.bussType)` 单独用会把它发出去）。
  if (values.bussType && values.bussType !== '-1') where.business_type = values.bussType
  // 成员类型：「全部」= -1
  if (values.userType && values.userType !== '-1') where.member_type = values.userType
  // D-0228 工作室分档：'false' 是合法显式值（只看打手），⛔ 不能真值判断（E-0021）。
  if (values.studioFlag === 'true') where.is_studio = true
  else if (values.studioFlag === 'false') where.is_studio = false
  // D-0228 改准二：打手身份按「代练员」职位（is_worker）；'false' = 内部员工档，同样是显式值。
  if (values.workerFlag === 'true') where.is_worker = true
  else if (values.workerFlag === 'false') where.is_worker = false
  if (values.isCert) where.is_cert = values.isCert
  // 禁止结算：Y = 已禁止(true) / N = 未禁止(false)；空 = 全部
  if (values.isDisSett === 'Y' || values.isDisSett === 'N') {
    where.settle_forbidden = values.isDisSett === 'Y'
  }
  if (values.key2) where.key2 = values.key2
  if (values.key3) where.key3 = values.key3
  return where
}

/**
 * `#tableList2`（接单统计卡片里的子表）的列定义，**逐条**照抄对标 `userlist.js` **L2440-2474**。
 *
 * 出处逐条核对过：8 列 = 勾选（`type:'checkbox'`, `fixed:'left'`, `width:30`）/ 排名 55 居中 /
 * 姓名-名称（**无宽度 ⇒ 自动列**）/ 接单量 140 居中 / 还未结算 120 居中 / 完单率 70 居中 /
 * 开通日期 120 居中 / 最近接单 120 居中。`sort` / `unresize` / `templet` 也逐字带过来
 * （`sort:true` 决定表头悬停的排序箭头、`unresize:false` 是对标显式写的默认值）。
 * ⚠️ 「姓名/名称」这一列**没有** `minwidth`（小写 w）这种拼写——它压根没有宽度字段，
 * 在 layui 眼里就是自动列。**不要**给它补 `width`/`minWidth`（本仓已有判例，见 `buildCols()`）。
 */
export function recTotalCols(): Col[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'idx', title: '排名', width: 55, align: 'center', sort: true, unresize: false },
    { field: 'userName', title: '姓名/名称', unresize: false },
    { field: 'orderCount', title: '接单量', width: 140, align: 'center', sort: true, unresize: false },
    { field: 'noSettAmount', title: '还未结算', width: 120, align: 'center', sort: true, unresize: false },
    {
      field: 'complateRate',
      title: '完单率',
      width: 70,
      sort: true,
      align: 'center',
      unresize: false,
      // 对标 `userlist.js:2458-2461` 的 templet 原文（`lay-tips` 也逐字照抄：它是 layuiadmin
      // 扩展的属性，我们 vendored 的 layui 里同样不生效，但别页已有「逐字照抄」先例）。
      templet: (res: { complateRate: number }) =>
        `<span title="订单完成率(只计算待验收+已完成的)">${res.complateRate}%</span>`,
    },
    { field: 'createUserDate', title: '开通日期', width: 120, align: 'center', sort: true, unresize: false },
    {
      field: 'lastRecOrderDate',
      title: '最近接单',
      width: 120,
      align: 'center',
      sort: true,
      unresize: false,
      // 后端窗口内没接过单时给的是字符串「无」（Task 3 已约定）；这里再兜一次空值——
      // 为空显示「无」，前端别指望后端永远不空。
      templet: (d: OrderStatsRow) => d.lastRecOrderDate || '无',
    },
  ]
}

/**
 * 接单统计卡片的**默认日期窗**：`今天 − 3 个自然月` ~ `今天`（`yyyy-MM-dd`、**含两端**）。
 *
 * 出处（值一律从这里取，别猜）：对标是**服务端渲染**进 HTML 的
 * `User__userlist.html` **L387** `#rsDate` = `2026-06-20`、**L398** `#reDate` = `2026-09-20`，
 * 而快照取自 **2026-09-20** ⇒ 2026-06-20 = 今天 − 3 个自然月，**日号保留 20**（同一族规律见
 * `order-orderlist.ts::defaultDateRange` 的注释）。对标页 JS 里那两处 `laydate.render`
 * （`userlist.js:616-630`）**既没有 `value` 也没有 `min`/`max`**，默认值不来自 JS。
 * ⚠️ 我们的标记里 `value=""`，所以默认值只能在**前端挂载时**算——就是本函数。
 *
 * ⚠️ **边界（本函数存在的第二个理由）**：`2026-05-31` 减 3 个月是 `2026-02-31`，**这一天不存在**。
 * 口径取「**该月最后一天**」⇒ `2026-02-28`（闰年 `2028-02-29`）。
 * ⛔ **不要**改成 `new Date(y, m - 3, d)` 然后直接用：JS 会把 `2 月 31 日` **溢出**成 `3 月 3 日`
 * （`order-orderlist.ts::defaultDateRange` 就是这么写的，那边有既有的单测钉住它的行为、本轮不动它），
 * 那会让「往前 3 个月」变成「往前 2 个月零 26 天」，默认窗口悄悄少几天。
 * 纯函数 + 单测钉住（`system-user-list.spec.ts`），**别把这段逻辑埋进 `laydate` 的 `done` 回调里**。
 */
export function defaultRecTotalRange(now: Date = new Date()): { stime: string; etime: string } {
  const pad = (n: number): string => String(n).padStart(2, '0')
  const fmt = (d: Date): string => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const year = now.getFullYear()
  const month = now.getMonth() - 3
  // `new Date(y, m + 1, 0)` = 目标月的**最后一天**（下个月的第 0 天），与目标月是几月无关。
  const lastDayOfTargetMonth = new Date(year, month + 1, 0).getDate()
  const day = Math.min(now.getDate(), lastDayOfTargetMonth)
  return { stime: fmt(new Date(year, month, day)), etime: fmt(now) }
}

/**
 * `#tableList3`（重复收款支付宝子账号检测卡片里的子表）的列定义，
 * **逐条**照抄对标 `userlist.js` **L2571-2603**：
 * 6 列 = 勾选 30 / 子账号名称 110 / 支付宝账号 170 左对齐 / 职位 85 居中 /
 * 重复数量 85 居中 / 当前状态 85 居中（全部有 `width`，没有 `minwidth` 那种拼写）。
 */
export function alipayCheckCols(): Col[] {
  return [
    { type: 'checkbox', fixed: 'left', width: 30 },
    { field: 'str1', title: '子账号名称', width: 110, unresize: false },
    { field: 'str2', title: '支付宝账号', width: 170, align: 'left', unresize: false },
    { field: 'str3', title: '职位', width: 85, align: 'center', unresize: false },
    {
      field: 'id2',
      title: '重复数量',
      width: 85,
      align: 'center',
      sort: true,
      unresize: false,
      // 对标 `userlist.js:2592-2596` 的 templet 原文。
      templet: (res: { id2: number }) =>
        `<span lay-tips="此收款支付宝账号存在${res.id2}个，该子账号是其中一个">${res.id2}</span>`,
    },
    {
      field: 'str4',
      title: '当前状态',
      width: 85,
      align: 'center',
      sort: true,
      unresize: false,
      // 对标 `userlist.js:2597-2600`：`'true'` ⇒ 蓝色「正常」，否则灰色「禁用」。
      templet: (res: { str4: string }) =>
        res.str4 === 'true'
          ? '<span class="layui-badge layui-bg-blue">正常</span>'
          : '<span class="layui-badge layui-bg-gray">禁用</span>',
    },
  ]
}

/**
 * `#tableList3`（支付宝查重）三种渲染结局的文案——**必须互不相同**（E-0004：空结果不等于没有数据）。
 *
 * | 结局 | `text.none` |
 * |---|---|
 * | 查出来有重复 | 表格有行，不显示空文案（值仍是 `none`，只是用不上） |
 * | 查出来**没有**重复 | `none` = 「没有重复的收款支付宝账号」 |
 * | 失败 / **无权限（403）** | `forbidden` = 「你没有查看重复收款支付宝的权限」 |
 *
 * ⚠️ 失败态**刻意不落进 `none`**：那个端点的权限节点是 `finance.payroll`，缺它回 403；
 * 若把 403 也渲染成「没有重复」，用户会得到「查过了、一切正常」的**假结论**。
 * ⚠️ 非 403 的失败用 `failed`（不把服务端的英文/技术性 `msg` 直接抛给用户）。
 */
export const ALIPAY_CHECK_TEXT = {
  none: '没有重复的收款支付宝账号',
  forbidden: '你没有查看重复收款支付宝的权限',
  failed: '检测失败，请稍后重试',
} as const

/**
 * 支付宝查重的一行（`#tableList3` 的 6 列里除了勾选列）。
 *
 * ⚠️ 字段名是**对标那套**（`str1`/`str2`/`str3`/`id2`/`str4`，见 `userlist.js:2571-2603`），
 * 因为列定义 `alipayCheckCols()` 的 `field` 与 templet 认的就是它们——这里换了名字列就空白。
 * `str4` 必须是**字符串** `'true'`/`'false'`：该列 templet 判的是 `res.str4 === 'true'`
 * （对标原文 `res.str4 == 'true'`），传布尔会一律渲染成灰色「禁用」。
 */
export type AlipayCheckRow = {
  /**
   * 子账号名称。⚠️ 2026-10-04（「显示人统一成昵称」线）起取**显示名**
   * （后端 `users[i].display_name`，昵称优先、空串回落登录账号），并**回落**老键 `username`
   * ——与 `emp-emplist.ts::AlipayRow.str1` 同一条口径（同一个后端键、同一张子表）。
   */
  str1: string
  str2: string
  str3: string
  id2: number
  str4: 'true' | 'false'
}

/**
 * 把 `fetchAlipayDuplicates()` 的**三级**出参摊成「**一行一个用户**」。
 *
 * 出参形状 `{duplicates: [{alipay_account, count, users:[{username, display_name?, job, is_active}]}]}`：
 * 一组重复的收款账户对应**多个**用户；表格每行是一个用户，该组的 `支付宝账号` 与 `重复数量`
 * 在**同组每行都相同**（对标表格就是这个粒度——「这个账号有 N 个子账号在用」）。
 */
export function buildAlipayCheckRows(result: AlipayDuplicatesResult): AlipayCheckRow[] {
  return result.duplicates.flatMap((group) =>
    group.users.map((user) => ({
      // ⚠️ 「子账号名称」这一格的语义是**人名** ⇒ 取**显示名**（昵称优先、空串回落登录账号），
      //    与绩效页 `emp-emplist.ts::alipayRows` 的 `str1` **同形**（同一个后端键、同一张子表）。
      //    `display_name` 缺席（后端未上，前端先上）时回落 `username`——老键仍在，
      //    它是「账号」这个事实；⛔ 不回落成空串。
      str1: user.display_name || user.username,
      str2: group.alipay_account,
      str3: user.job,
      id2: group.count,
      str4: user.is_active ? ('true' as const) : ('false' as const),
    })),
  )
}

/** `#tableList3` 的 `table.render` 实参；`data` 由挂载处按取数结果覆盖（`url` 仍然不写）。
 *
 * 出处：对标 `userlist.js:2572-2605` 的 `table.render`——本任务按 brief 把它**有意留给你**的
 * 三项一并补齐：
 * - `page: false`（这张表不分页）；
 * - `defaultToolbar: ['filter']`（对标的同一个对象字面量里写了两遍 `[]` 后跟 `['filter']`，
 *   **后写生效**；只写一次，别把那个盖掉的前值也抄进来）；
 * - **没有 `limit` / `limits`**：`page:false` 之下它们没有落点，对标原文也确实没有这两个键
 *   （主表那 6 项 `[10,20,30,40,50,100]` 是**别处**的值，别顺手搬过来）。
 * ⚠️ `data` 走我们自己的 `fetchAlipayDuplicates()`（见 `buildAlipayCheckRows`），
 *   仍然**不写** layui 的 `url`。
 */
export function alipayCheckTableOptions(): Record<string, unknown> {
  return {
    elem: '#tableList3',
    data: [],
    toolbar: '#toolbarDemo3',
    defaultToolbar: ['filter'],
    page: false,
    text: { none: ALIPAY_CHECK_TEXT.none },
    cols: [alipayCheckCols()],
  }
}

/**
 * 两张子表工具栏的**唯一差异**：最后那行橙字（对标 `User__userlist.html` **L415** / **L431**，逐字）。
 * 两句**必须不同**——它们分别说明「接单统计只统计正常子账号（有 2 分钟缓存）」与
 * 「重复检测包含所有状态的子账号」。
 */
export const SUBTABLE_TOOLBAR_NOTES = {
  recTotal:
    '这里已排除了禁用的子账号，只统计有效状态为正常的子账号。注意，数据有2分钟缓存，禁用账号、删除子账号均会有缓存',
  aliPay: '这里包含了系统所有状态下的子账号',
} as const

/**
 * 一张子表的工具栏模板（对标 `User__userlist.html` **L404-417** / **L420-433**）。
 *
 * 三颗按钮的 `class`/`style`/`type`/文案**逐字**照抄对标（`刷新列表` 默认绿 / `禁用子账号` 默认绿 /
 * `删除用户` 红 `layui-btn-danger`）；`title="刷新当前数据"` 只在第一颗上。
 *
 * ⚠️ **故意把对标的 `lay-event="refresh2"/"disBtn"/"delUserBtn2"` 换成 `data-sub="同原值"`**：
 * 原因与主工具栏那颗长注释完全相同——layui 的表格工具栏对 `*[lay-event]` 做委托点击并
 * `return false`，我们挂在 `root` 上的委托就收不到，按钮会**点了没反应**。这里用 `data-sub`
 * 把对标的原值记下来（下一个任务接点击时按它分派），点击统一提示「尚未开放」。
 * ⚠️ 对标第一颗按钮还带 `lay-tips="勾选子账号，将其禁用…"`——与页内工具栏的既有处置一致
 * （`lay-tips` 是 layuiadmin 扩展的属性，我们 vendored 的 layui 里不生效），不复刻。
 * ⚠️ 第二行那个**空 `id=""`** 是对标原文就有的，照抄。
 */
function subtableToolbarTemplate(id: 'toolbarDemo2' | 'toolbarDemo3', note: string): string {
  return `<script type="text/html" id="${id}">
                                <div class="layui-btn-container">
                                    <button class="layui-btn layui-btn-xs" style="margin-right:5px" type="button" title="刷新当前数据" data-sub="refresh2">刷新列表</button>
                                    <button class="layui-btn layui-btn-xs" style="margin-right:5px" type="button" data-sub="disBtn">禁用子账号</button>
                                    <button class="layui-btn layui-btn-xs layui-btn-danger" style="margin-right:5px" type="button" data-sub="delUserBtn2">删除用户</button>
                                </div>
                                <div style="color:#ff6a00" id="">${note}</div>
                            </script>`
}

/**
 * 页内那 4 个 `display:none` 的容器，**逐字**取自对标 `User__userlist.html` L305-420：
 * `.recPricebox`（设抢单价范围弹层）、`.typebox`（业务类型弹层）、
 * `.layui-card.recTotalDiv`（接单统计卡片，内含 `#tableList2` 与 `#toolbarDemo2`）、
 * `.layui-card.checkAliPayAccDiv`（支付宝重复检测卡片，内含 `#tableList3` 与 `#toolbarDemo3`）。
 *
 * **为什么必须带上它们**（哪怕一直 `display:none`）：
 * 1. 对标把它们放在 `<form class="layui-form">` 里（L111 开 → L421 闭），页内 `<style>` L89-104 的
 *    `.layui-input-inline{overflow:visible!important}` 等规则就是为它们写的；
 * 2. 探针按**文档级选择器**统计而不是「可见元素」：删掉它们，`.layui-form-label` 会从 15 掉到 10、
 *    `.layui-input-inline` 从 16 掉到 11（差异 #1 的「条数」项就是这么来的）。
 *    （这两个数是**本页当前**的：筛选栏 10 个 + 隐藏容器 5 个；对标是 11+5 / 11+1+5，
 *    差的那 1 就是业务方 2026-09-22 裁掉的「组群状态」。）
 *
 * 两张内表（`#tableList2`/`#tableList3`）**只放标记、不调 `table.render`**：实测对标页在初始状态下
 * 也只渲染了 1 张表（`probe-bench.json` 的 `ths` 恰好 29 个单元格，若有第二张表会翻倍），
 * 它们的 `table.render` 是在点开卡片时才发生的。我们照此办理。
 *
 * ⚠️ 两处**有意偏离对标原文**（都不是笔误）：
 * 1. `#tableList2` / `#tableList3` 后面各跟一个 `<script type="text/html" id="toolbarDemo2/3">`
 *    子工具栏模板（对标 L404-417 / L420-433），按钮的 `lay-event` 换成 `data-sub`——理由见
 *    `subtableToolbarTemplate()`；模板里的按钮在 `<script>` 里，**不参与**文档级
 *    `.layui-form-label` / `.layui-input-inline` / `button` 计数。
 * 2. 卡片内那个「游戏权限」下拉，对标是 `id="gameName" name="gameName" lay-filter="selectGameTotal"`
 *    （L352）——与主筛选栏的 `#gameName`（L117）**撞 id 又撞 name**。我们把 `id` 改成唯一值
 *    `recTotalGameName`（照对标保留 `lay-filter="selectGameTotal"`），`name` 改成 `gameNameTotal`：
 *    同名会让 `form.val('sform')` 读到卡片里的值，把主筛选栏的「游戏权限」筛选**静默换掉**（E-0027 同型）。
 *    ⚠️ 两个日期框的 `id`（`rsDate` / `reDate`）与 `id="recTotalGameName"` 是**真实取值口**：
 *    `recTotalWhere()` 按 id 从 `document` 取（弹层打开时节点不在 `root` 之下），
 *    `laydate.render({elem:'#rsDate'})` 也按 id 绑——少一个 id 就是「改了日期/游戏但筛选不生效」。
 */
function hiddenContainersHtml(games: ConfigRow[]): string {
  // 对标 recTotalDiv 里的「游戏权限」下拉是**按游戏名**取值（`<option value="原神">原神</option>`），
  // 不是 id —— 与主筛选栏那个按 id 取值的 `#gameName` 同名但语义不同（对标原文 L347-365）。
  // ⚠️ **只送有效游戏名**：`games` 与主筛选栏**同一个来源**（挂载时的 `fetchAllGames()`，
  // 软删除的游戏后端已排除，见 `config_views.SoftDeleteConfigMixin.get_queryset`）——别在这里
  // 另起一份游戏列表。
  const gameNames = games.map((g) => `<option value="${g.name}">${g.name}</option>`).join('\n')
  return `
    <div class="recPricebox" style="display:none;">
      <div class="layui-form-item" style="margin-top:20px">
        <div class="layui-inline">
          <label class="layui-form-label">接单价范围</label>
          <div class="layui-input-inline" style="width: 100px;">
            <input type="text" name="price_min" id="price_min" placeholder="￥" autocomplete="off" class="layui-input" value="0">
          </div>
          <div class="layui-form-mid">-</div>
          <div class="layui-input-inline" style="width: 100px;">
            <input type="text" name="price_max" id="price_max" placeholder="￥" autocomplete="off" class="layui-input" value="99999">
          </div>
        </div>
      </div>
      <div class="layui-form-item" style="text-align:center;margin:0 auto">
        <a href="javascript:void(0)" class="layui-btn layui-btn-normal setRecPriceSaveBtn" style="margin-top:20px;margin-bottom:15px; width:150px">保存</a>
      </div>
    </div>

    <div class="typebox" style="display:none;">
      <div class="layui-form-item" style="padding-top:20px;">
        <label class="layui-form-label" style="width:130px;">选择业务类型：</label>
        <div class="layui-input-block">
          ${businessTypeRadiosMarkup()}
        </div>
      </div>
      <div class="layui-form-item" style="text-align:center;margin:0 auto">
        <a href="javascript:void(0)" class="layui-btn layui-btn-normal settypes" style="margin-top:50px;margin-bottom:30px; width:150px">保存</a>
      </div>
    </div>

    <div class="layui-card recTotalDiv" style="display: none; height: 100%; overflow: auto; ">
      <div class="layui-card-body">
        <div class="layui-form-item">
          <div class="layui-inline">
            <label class="layui-form-label" style="width: 80px">游戏权限</label>
            <div class="layui-input-inline" style="">
              <select id="${REC_TOTAL_GAME_SELECT_ID}" name="gameNameTotal" lay-filter="${REC_TOTAL_GAME_FILTER}">
<option value="">全部</option>
${gameNames}
              </select>
            </div>
          </div>
          <div class="layui-inline">
            <div class="layui-form-pane">
              <label class="layui-form-label" style="width: 80px; padding: 8px 5px">接单开始日</label>
              <div class="layui-input-inline" style="width:105px">
                <input type="text" name="rsDate" id="${REC_TOTAL_START_ID}" placeholder="" autocomplete="off" class="layui-input" value="">
              </div>
            </div>
          </div>
          <div class="layui-inline">
            <div class="layui-form-pane">
              <label class="layui-form-label" style="width: 80px;padding:8px 5px">接单结束日</label>
              <div class="layui-input-inline" style="width:105px">
                <input type="text" name="reDate" id="${REC_TOTAL_END_ID}" placeholder="" autocomplete="off" class="layui-input" value="">
              </div>
            </div>
          </div>
        </div>
        <div style="">
          <table class="layui-hide" id="tableList2" lay-filter="tableList2"></table>
          ${subtableToolbarTemplate('toolbarDemo2', SUBTABLE_TOOLBAR_NOTES.recTotal)}
        </div>
      </div>
    </div>

    <div class="layui-card checkAliPayAccDiv" style="display:none">
      <div class="layui-card-body">
        <div style="">
          <table class="layui-hide" id="tableList3" lay-filter="tableList3"></table>
          ${subtableToolbarTemplate('toolbarDemo3', SUBTABLE_TOOLBAR_NOTES.aliPay)}
        </div>
      </div>
    </div>`
}

/**
 * **本页自己的**页内覆盖（对标 `User__userlist.html` 的内联 `<style>`，**逐字搬**）。
 *
 * 为什么在页面里而不是 `layui/island.ts` 的 `ISLAND_OVERRIDES`：这两组规则**只出自这一页的
 * 页内 `<style>`**，不是三个全局样式表（`layui.css` / `admin.css` / `zdy.css`）里的值。
 * 2026-09-20 之前它们在 `ISLAND_OVERRIDES` 里、被当成了全站值，代价是：
 * **页内没有这条覆盖**的对标页（实测 `/System/AutomaticAdJustPriceSet`）表头高 **41 vs 对标 39**、
 * 整表与 fieldset 一起 +2px——那些页生效的是对标 `layuiadmin/layui/css/layui.css` 的
 * `.layui-table-cell{height:38px;line-height:28px;padding:6px 15px}`
 * （与我们 vendored 的 `public/layui/css/layui.css` **逐字相同**）。搬回来后两边同时对上。
 *
 * 出处（`User__userlist.html`）：
 * - `.layui-table-cell{…}` **L53-64**（对标原文：`height:40px;line-height:40px;padding:0 5px;…font-size:12px`）；
 * - `.layui-form-item` / `.layui-input-inline` / `.layui-form-pane` / `.layui-layer-content`
 *   四条 `{overflow:visible!important}` **L88-104**（对标原文是 **4 条独立规则**，不是一条并写）。
 *
 * 放进 `pageHtml()` 的**最前面**：`ISLAND_OVERRIDES` 与 `layui.css` 都在 `<head>`，
 * 页内 `<style>` 在 `#app` 里、文档顺序在后 ⇒ 同特异性时它胜出（与对标「页内 `<style>` 在
 * `<body>`、全局 CSS 在 `<head>`」的顺序一致）。对标该页 `<style>` 里另有 `.lableb`（与全局
 * `zdy.css` 同值，我们已由 `ISLAND_OVERRIDES` 覆盖）与 `.qyBoxTitle`/`.qyBoxCont`
 * （该页标记里根本没用到，是死规则），都不需要在这里重复。
 */
const PAGE_LOCAL_STYLE = `
    .layui-table-cell {
        height: 40px;
        line-height: 40px;
        padding: 0 5px;
        position: relative;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        box-sizing: border-box;
        font-size: 12px;
    }

    /* 撑开或允许下拉框溢出显示 */
    .layui-form-item {
        overflow: visible !important;
    }

    .layui-input-inline {
        overflow: visible !important;
    }

    .layui-form-pane {
        overflow: visible !important;
    }

    /* 如果是 layer 弹窗导致的，允许弹窗主体内容溢出 */
    .layui-layer-content {
        overflow: visible !important;
    }
`

/**
 * 整页标记。**`<form class="layui-form">` 包住整页**（筛选栏 → 表格 → 4 个隐藏容器），
 * 与对标 `User__userlist.html` **L111 开、L421 闭**一致。
 *
 * 上一版只把**筛选栏**包进 form，于是探针里 `form.box.h` 对标 **1095** / 我们 **86**——
 * 86 正好只是筛选栏那一坨的高度。`form` 的高度差会连带影响任何依赖「form 从哪开始量」的判断。
 *
 * 保留 `onsubmit="return false"`：对标 `<form action="">` 没有它，但他们的按钮由 layui 的
 * `lay-submit` 处理器接管；我们的「保存」类按钮没有后端，留着这行是**防止 `action=""` 触发真提交
 * 刷新整页**（行为防线，不影响任何视觉值）。
 */
/**
 * 身份切换条（2026-10-12，业务方要求「把打手 / 工作室 / 内部员工区分开，方便人事只看某一身份」）。
 *
 * ## 实现口径与如实登记的边界
 *
 * 三类身份里**只有两类有数据源**：`StaffProfile.member_type`（`0` 内部成员 / `1` 合作用户，
 * `apps/accounts/choices.py`）——后端列表本就收 `member_type` 过滤（`buildUserListWhere`
 * 的 `userType` 行）。tab 条是**驱动既有筛选**的快捷入口：点 tab = 设 `userType` + 按同一份
 * `buildUserListWhere` 重取（与手改下拉同一参数、同一出口，⛔ 不开第二套参数）。
 *
 * ⭐ **改准一（2026-10-12 上午，D-0228）**：新增 `StaffProfile.is_studio`（工作室独立字段）；
 * 「在开关那里加一个」那条待决被业务方撤回。
 * ⭐ **改准二（2026-10-12 上午，业务方报「打手/工作室两档全 0」后修正）**：分类**不再看成员类型**
 * （对标自己的 userType tooltip 写着「该选项并无实际作用」，且存量账号全是默认值「内部成员」）——
 * **打手 = 持「代练员」职位且未标工作室**（`is_worker`，系统标准职位 `apps/business/roles.py::WORKER`，
 * 历史名就叫「打手」）；**工作室 = 勾了 is_studio（显式标记优先，职位/成员类型都不看）**；
 * 内部员工 = 其余。手动的成员类型下拉仍按原语义过滤，不受影响。
 * ⚠️ 同步是**单向的**（tab → 下拉值）：手改 `userType` 下拉不会回移 tab 高亮（v1 不做
 * 双向绑定；下拉本身照常可用）。
 */
export const IDENTITY_TAB_CLASS = 'identityTab'
export const IDENTITY_TAB_ACTIVE_CLASS = 'identityTabActive'
export const IDENTITY_TAB_ATTR = 'data-identity-tab'
/**
 * ⭐ 身份四档（2026-10-12 业务方拍板，D-0228）：内部员工 = `member_type=0`；
 * 打手 = 合作用户**且未勾工作室**（原话「只要不是工作室的打手都算打手」）；
 * 工作室 = 合作用户**且勾了工作室**（`StaffProfile.is_studio`，独立字段）。
 * `studioFlag` 是筛选表单里的隐藏域（`-1` = 全部），与 `userType` 一起经
 * `buildUserListWhere` 落到后端 `member_type` + `is_studio` 两个参数——同一出口。
 */
export const IDENTITY_TABS = [
  { key: 'all', label: '全部', userType: '-1', workerFlag: '-1', studioFlag: '-1' },
  { key: 'internal', label: '内部员工', userType: '-1', workerFlag: 'false', studioFlag: '-1' },
  { key: 'taker', label: '打手', userType: '-1', workerFlag: 'true', studioFlag: 'false' },
  { key: 'studio', label: '工作室', userType: '-1', workerFlag: '-1', studioFlag: 'true' },
] as const

/** 身份切换条的标记（纯函数；样式内联，与 `PAGE_LOCAL_STYLE` 同一份页面内 `<style>` 体系）。 */
export function identityTabsHtml(): string {
  const chips = IDENTITY_TABS.map(
    (tab) =>
      `<button type="button" class="layui-btn layui-btn-primary ${IDENTITY_TAB_CLASS}" ` +
      `${IDENTITY_TAB_ATTR}="${tab.key}" style="margin-right:6px">${tab.label}</button>`,
  ).join('')
  return `<div style="margin:0 0 8px 0" id="identityTabBar">${chips}</div>`
}

function pageHtml(roles: string[], games: ConfigRow[], showDepositQuota = false): string {
  return `
  <style type="text/css">
    ${PAGE_LOCAL_STYLE}
  </style>
  <form class="layui-form" action="" lay-filter="sform" onsubmit="return false">
    ${identityTabsHtml()}
    <!-- D-0228：身份分档的隐藏域（-1=全部/false=非工作室/true=工作室）；由身份切换条写入，
         buildUserListWhere 读它发 is_studio。藏在这里而不是组件状态里，是为了与 userType
         走同一个 form.val 出口（手改 userType 下拉后点搜索也不丢这一格）。 -->
    <input type="hidden" name="studioFlag" value="-1">
    <input type="hidden" name="workerFlag" value="-1">
    ${filtersHtml(roles, games)}
    <div style="">
      <table class="layui-hide" id="${TABLE_ID}" lay-filter="${TABLE_FILTER}"></table>
    </div>
    ${hiddenContainersHtml(games)}
  </form>
  <script type="text/html" id="userToolbarTpl">${toolbarHtml(showDepositQuota)}</script>`
}

/** 导出给 spec 校验「`<form>` 包住整页」这条结构（对标 L111-421）。 */
export const pageHtmlForTest = pageHtml

/**
 * 当前登录人有没有「打手钱包调账」节点——「押金额度」按钮**画不画**的判据。
 *
 * 为什么在人员管理页再加一道门：后端端点自己带 `has_node("finance.taker_wallet_adjust")`
 * （计划 §5.1），而那又是**本页自有**按钮（对标没有）⇒ 照 `AGENTS.md` §5-D / D-0154 / E-0098
 * **按节点渲染、没权限就不画**，别让用户点出一个 403。
 *
 * **fail-closed**：`fetchMe()` 失败（401 / 网络）⇒ `null` ⇒ 不画按钮。
 * 这条不是「防御性写法」——`system-user-list.interaction.spec.ts` 有一条「身份请求失败 ⇒ 不渲染」
 * 的用例钉着它（去掉 `catch` 那条会红）。
 */
async function viewerCanAdjustTakerWallet(): Promise<boolean> {
  let me: User | null = null
  try {
    me = await fetchMe()
  } catch {
    me = null
  }
  return me ? canNode(me, TAKER_DEPOSIT_QUOTA_NODE) : false
}

/**
 * 挂载页面。返回卸载函数（切路由时调用）。
 *
 * 交互与对标的对应：14 个工具栏按钮里 8 个接我方后端，其余 6 个 `layer.msg('尚未开放')`；
 * 其余写操作（启停/重置/标记/游戏权限/业务类型/抢单价）也都落到 `/api/accounts/`。
 */
export async function mountUserListPage(
  root: HTMLElement,
  // ⚠️ Task 13 之后本页内**不再有跳页**：原先「新增」「编辑」两颗出口都改成
  //    `layer.open({type:2})` 打开 `pages/user-adduser.ts` 弹层，`onNavigate` 在本页里
  //    再无调用方（`/system-users/new`、`/system-users/:id/edit` 两条路由**保留**，
  //    由菜单 / 直接访问进入）。参数本身保留：`mount` 与各测试都按位置传它，签名是对外契约；
  //    按本仓约定用 `_` 前缀标注「有意保留、暂未使用」（`eslint.config.ts` 的 `argsIgnorePattern`）。
  _options: UserListPageOptions,
): Promise<() => void> {
  // `bootstrapIsland` = 加载 layui + 注入对标覆盖样式（顺序有讲究，见 `layui/island.ts`）。
  // `dropdown` 是「检测功能」那颗按钮的**二级下拉**用的模块（对标 `userlist.js:1280` 的
  // `dropdown.render({elem:'#checkDropdown'})`）——不加进 `layui.use` 列表，`layui.dropdown`
  // 就是 undefined，点了**毫无反应且不报错**。
  // `laydate` 是接单统计卡片那两个日期框要的（对标 `layui.use([... 'laydate' ...])`，合并 main 带入）。
  const layui: LayuiNamespace = await bootstrapIsland(['form', 'table', 'layer', 'dropdown', 'laydate'])
  const form = layui.form as LayuiModule
  const table = layui.table as LayuiModule
  const layer = layui.layer as LayuiModule
  const dropdown = layui.dropdown as LayuiModule
  const laydate = layui.laydate as LayuiModule

  // 桌面/移动的判据，**逐字**照抄对标 `userlist.js:596-603`（`layui.device()` 之后看
  // `android === true || ios === true`）。实测本仓 vendored 的 layui 2.13.9 有 `layui.device`：
  // 桌面返回 `{os:'mac',ie:false,weixin:false,android:false,ios:false,mobile:false}` ⇒ `isPc = true`。
  // 它只决定两个弹层的 `area`（见 `typeDialogArea` / `recPriceDialogArea`）。
  const device = (layui as unknown as { device: () => { android?: boolean; ios?: boolean } }).device()
  const isPc = !(device.android || device.ios)

  // layui **自带** jQuery：本页（island）没有 import jquery，而 `layer.open` 的 `content` 必须传
  // **jQuery 对象**——传裸 `HTMLElement` 会让 layui 抛 `TypeError: r.parents is not a function`、
  // 弹层根本不出来（实测记录见 `config-replica.ts` 的 `openPanelLayer` 注释）。
  // 实测 `typeof layui.$ === 'function'`。这里只声明我们用到的那一下：`hide()`。
  const $ = (layui as unknown as { $: (selector: string) => { hide: () => void } }).$

  // ⚠️ 取**全量**（`fetchAllGames`）而不是按权限收窄的 `fetchGames()`：本页那次取数同时喂
  // 三处——主筛选栏、列表「游戏权限」列的名字映射、以及「设置游戏权限」弹窗的多选。弹窗要能
  // 勾上账号**当前没有**的游戏（含给自己减权限之外的授权），收窄了就是死锁
  // 「看不到没勾的游戏 ⇒ 也就勾不上」（计划 DoD #4）。
  // ⭐ 第三个是「押金额度」那颗**自有**按钮的权限门（见 `viewerCanAdjustTakerWallet`）。
  const [roles, games, showDepositQuota] = await Promise.all([
    fetchAccountRoles(),
    fetchAllGames(),
    viewerCanAdjustTakerWallet(),
  ])
  const gameNames = new Map(games.map((g) => [g.id, g.name]))

  // 页标题：对标是**服务端渲染**的 `<title>系统用户管理</title>`（`User__userlist.html` L5）。
  // island 的外壳页只有一个静态 `<title>团团通</title>`（全站共用），所以每个页面模块挂载时自己设。
  document.title = '系统用户管理'

  root.innerHTML = pageHtml(roles, games, showDepositQuota)

  // 三个多选下拉交给对标的 formselects-v4 渲染（`.xm-select-parent`；对标 `userlist.js` 靠
  // `layui.use(['formSelects'])` 模块加载时自动渲染）。资产与初始化入口见 `@/layui/vendor`：
  // `bootstrapIsland()` 已 `await ensureBenchmarkVendor()`，所以这里直接渲染即可。
  // DOM 必须已在（插件要把 `.xm-select-parent` 插到 `<select>` 之后），故排在 `root.innerHTML` 之后；
  // 插件会给原 `<select>` 打上 `lay-ignore`，之后 `form.render()` 不会再动它（源码 `Common.prototype.init`）。
  // 顺序**不**影响结果：插件自己会先移除 layui 已渲染的 `.layui-form-select`。
  initFormSelects('select[xm-select]')

  const msg = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
  }
  const notOpen = (label: string): void => msg(`「${label}」尚未开放`)

  const reload = (extra: Record<string, unknown> = {}): void => {
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, { page: { curr: 1 }, ...extra })
  }

  const checkedRows = (): AccountRow[] =>
    ((table.checkStatus as (id: string) => { data: AccountRow[] })(TABLE_ID).data ?? []) as AccountRow[]

  const requireChecked = (): AccountRow[] | null => {
    const rows = checkedRows()
    if (rows.length === 0) {
      // 逐字照抄对标：`userlist.js` 的 i18n `tableList_user: '请勾选要操作的数据行'`（该页 :329），
      // 9 处 `layer.msg` 调用点用的都是这一句（`:1048` … `:1152`）。
      // ⚠️ 本页原先写的是**自拟**的「请先勾选账号」（整体评审 Minor 13 点名，2026-09-23 收口时对齐）。
      msg('请勾选要操作的数据行')
      return null
    }
    return rows
  }

  /**
   * 勾选行**逐行**PATCH：载荷由 `build(row)` 按行算。返回**是否真的提交成功**——
   * 「设置业务类型」「设抢单价范围」「设自助接单数」要在成功时关掉弹层
   * （对标成功回调里 `layer.close(settypeLayer)`），所以需要这个返回值。
   *
   * ⚠️ 与 `patchSelected` 分开的原因：「禁/解结算」是**逐行各自取反**（对标
   * `UpUserDisSettSta` 按 uid 翻转），不是「所有行统一设成一个值」——那会把这个功能做错
   * （混合勾选时一半行会被写成反的值）。需要统一值时用下面那个薄封装。
   */
  async function patchEachRow(
    build: (row: AccountRow) => Partial<AccountWritePayload>,
    okMsg: string,
  ): Promise<boolean> {
    const rows = requireChecked()
    if (!rows) return false
    try {
      await Promise.all(rows.map((r) => updateAccount(r.id, build(r))))
      msg(okMsg)
      reload()
      return true
    } catch (e) {
      msg(e instanceof Error ? e.message : '保存失败')
      return false
    }
  }

  /** 勾选行批量 PATCH**同一个载荷**（大部分批量动作是这个形状）。 */
  function patchSelected(patch: Partial<AccountWritePayload>, okMsg: string): Promise<boolean> {
    return patchEachRow(() => patch, okMsg)
  }

  /**
   * 页内两个对标弹层的**层索引**（保存成功后 `layer.close` 用）。`undefined` = 当前没开着。
   */
  let typeLayerIndex: number | undefined
  let recPriceLayerIndex: number | undefined

  const closeLayer = (index: number | undefined): void => {
    if (typeof index === 'number') (layer.close as (i: number) => void)(index)
  }

  /** 对标 `end` 回调：`$('.typebox').hide()` / `$('.recPricebox').hide()`。 */
  const hideTypebox = (): void => {
    $('.typebox').hide()
  }
  const hideRecPricebox = (): void => {
    $('.recPricebox').hide()
  }

  /**
   * 「设置业务类型」弹层 —— 实参见 `TYPE_DIALOG_BASE` / `typeDialogArea`（对标 `userlist.js:1097-1109`）。
   *
   * ⚠️ 用**页内那个 `.typebox` 容器**（`content: $('.typebox')`），不是自造标记：业务方
   * 2026-09-22 的原话是「设置游戏权限和业务类型的弹窗没有复刻成功」，根因就是旧实现另开了一个
   * 通用弹层（`.layer-form`）而不是搬出对标那个容器。
   * ⚠️ 实测：`layer.open` 会把容器**搬进** `.layui-layer`，关闭时 layui 会把它**放回原位**
   * （父链仍是 `FORM.layui-form`），所以 `end` 里只需 `hide()`，不必像 `emp-emplist.ts` 那样手动搬回。
   */
  const openTypeDialog = (): void => {
    typeLayerIndex = (layer.open as (o: unknown) => number)({
      ...TYPE_DIALOG_BASE,
      area: typeDialogArea(isPc),
      content: $('.typebox'),
      end: () => {
        hideTypebox()
      },
    })
  }

  /**
   * 「设抢单价范围」弹层 —— 实参见 `REC_PRICE_DIALOG_BASE` / `recPriceDialogArea`
   * （对标 `userlist.js:1136-1148`）。容器与 `end` 的理由同上。
   */
  const openRecPriceDialog = (): void => {
    recPriceLayerIndex = (layer.open as (o: unknown) => number)({
      ...REC_PRICE_DIALOG_BASE,
      area: recPriceDialogArea(isPc),
      content: $('.recPricebox'),
      end: () => {
        hideRecPricebox()
      },
    })
  }

  /**
   * `.typebox` 的「保存」：校验照对标 `userlist.js:1626-1630` ——
   * `input[name=selbussType]:checked` 取不到值（或值为空串）⇒ 提示并 **return**。
   *
   * ⚠️ **不预选**（`businessTypeRadiosMarkup` 的三个 radio 都没有 `checked`）：勾选多行且各行
   * 业务类型不同时没有一个值能代表全体；预选任意一项都会把存量行**静默改值**（E-0027 同型）。
   * 「没选就保存」由上面那句拦住——这就是本页选的那一种做法（另一种是预选当前值）。
   */
  async function onSaveBusinessType(): Promise<void> {
    const checked = root.querySelector<HTMLInputElement>('input[name=selbussType]:checked')
    const value = checked?.value ?? ''
    if (value === '') {
      msg(DIALOG_MESSAGES.businessTypeRequired)
      return
    }
    const ok = await patchSelected({ business_type: Number(value) }, '业务类型已保存')
    if (ok) {
      closeLayer(typeLayerIndex)
      typeLayerIndex = undefined
    }
  }

  /**
   * `.recPricebox` 的「保存」：6 条校验见 `recPriceValidationMessage`（对标 `userlist.js:1661-1687`）。
   * 取值走 **id**（`#price_min` / `#price_max`），与对标 `$("#price_min").val()` 一致。
   * 通过 ⇒ 两个值**一起** PATCH（只给一个会把另一个留在空值上），关层、刷新。
   */
  async function onSaveRecPrice(): Promise<void> {
    // 取值走 **id**（`#price_min` / `#price_max`），与对标 `$("#price_min").val()` 一致。
    const min = (document.getElementById('price_min') as HTMLInputElement | null)?.value ?? ''
    const max = (document.getElementById('price_max') as HTMLInputElement | null)?.value ?? ''
    const problem = recPriceValidationMessage(min, max)
    if (problem !== null) {
      msg(problem)
      return
    }
    const ok = await patchSelected({ grab_price_min: min, grab_price_max: max }, '抢单价范围已保存')
    if (ok) {
      closeLayer(recPriceLayerIndex)
      recPriceLayerIndex = undefined
    }
  }

  /**
   * 两颗页内弹层「保存」的**委托**点击（`.settypes` / `.setRecPriceSaveBtn`）。
   *
   * ⚠️ 监听挂在 `root` 上（与其余委托一致）：实测 `layer.open` 把容器搬进 `.layui-layer` 之后，
   * 那一层**仍在页内 `<form class="layui-form">` 里**（父链
   * `.layui-layer-content → .layui-layer → FORM.layui-form → #app`），所以 root 上的委托收得到。
   * ⚠️ 用**原生委托**而不是 jQuery 的 `.on`：本页只保证有 layui 自带的 `$`，不去依赖它的
   * 事件委托实现（对标用的是完整 jQuery）。
   */
  const onDialogSaveClick = (event: Event): void => {
    const el = (event.target as HTMLElement).closest<HTMLElement>('.settypes, .setRecPriceSaveBtn')
    if (!el) return
    if (el.classList.contains('settypes')) {
      void onSaveBusinessType()
      return
    }
    void onSaveRecPrice()
  }

  /**
   * 开一张卡片（「接单统计」/「重复收款支付宝子账号检测」共用）——对标 `userlist.js:1255-1274`
   * 与 `1287-1312` 是**同一段流程**（只有 `title` 与 `content` 不同）：
   * `layer.open(cardDialogArgs(...))` → 非 PC 端再 `layer.full(index)` → 渲染子表。
   *
   * ⚠️ `content` 必须是 **jQuery 对象**（`$(容器选择器)`），不是裸 DOM——见 `$` 的注释。
   * ⚠️ `cancel` / `end` 都只做一件事：把容器**藏回去**（对标原文 `$('.xxx').hide()`）。
   *    `layer.open` 会把容器搬进 `.layui-layer`，关闭时 layui 自己把它放回原位，
   *    所以这里不需要像 `emp-emplist.ts` 那样手动搬回。
   * ⚠️ `layer.full` 在**非 PC** 端才调（对标原文 `if (!isPc) { layer.full(rankLayer) }`）。
   */
  const openCard = (title: string, containerSelector: string, render: () => void): void => {
    const index = (layer.open as (o: unknown) => number)({
      ...cardDialogArgs(title, isPc),
      content: $(containerSelector),
      cancel: () => {
        $(containerSelector).hide()
      },
      end: () => {
        $(containerSelector).hide()
      },
    })
    if (!isPc) (layer.full as (i: number) => void)(index)
    render()
  }

  /** 点开卡片后才渲染子表（`data: []` + 空数据文案，见 `*TableOptions`）。 */
  const renderSubTable = (options: Record<string, unknown>): void => {
    ;(table.render as (o: unknown) => void)(options)
  }

  /* ────────────────────── 接单统计卡片（`.recTotalDiv`） ────────────────────── */
  /*
   * 接 `GET /api/business/order-stats`。窗口、行集合、完成率、最近接单日的口径都在端点那一侧，
   * 前端只做取数、渲染和两个筛选。
   * 卡片外壳、两颗卡片的 `layer.open` 实参、子表工具栏（`#toolbarDemo2`）用 `data-sub`，
   * 不是对标的 `lay-event`。
   */

  /** 卡片表格**是否已经 `table.render` 过**（layui 的 `reload` 只能用在已渲染的实例上）。 */
  let recTotalRendered = false

  /**
   * 找卡片容器。⚠️ **不能只用 `root.querySelector`**：真 layui 的弹层会把节点**搬走**，
   * 关闭时还会把它**从文档里摘掉**——这正是「第二次点开静默不出现」的成因。查找顺序：弹层里 → `root`。
   */
  function recTotalPanel(): HTMLElement | null {
    return (
      document.querySelector<HTMLElement>(`.layui-layer ${REC_TOTAL_PANEL_SELECTOR}`) ??
      root.querySelector<HTMLElement>(REC_TOTAL_PANEL_SELECTOR)
    )
  }

  /**
   * 卡片里那个控件（下拉/日期输入）的当前值。
   * **按 id 从 `document` 取**：弹层打开时节点不在 `root` 之下，`root.querySelector` 取不到。
   */
  function recTotalInputValue(id: string): string {
    return (document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null)?.value ?? ''
  }

  /** 卡片三个控件 → 端点参数。空串一律**不送**（`gameName=''` 在后端本来也按「没传」处理）。 */
  function recTotalWhere(): Record<string, string> {
    const where: Record<string, string> = {}
    const stime = recTotalInputValue(REC_TOTAL_START_ID)
    const etime = recTotalInputValue(REC_TOTAL_END_ID)
    const gameName = recTotalInputValue(REC_TOTAL_GAME_SELECT_ID)
    if (stime !== '') where.stime = stime
    if (etime !== '') where.etime = etime
    if (gameName !== '') where.gameName = gameName
    return where
  }

  /**
   * 卡片取数的**唯一入口**。
   *
   * `resetPage` 在两个调用点上语义**刻意不同，别统一**（照对标）：
   *  - **点开卡片 / 改日期** ⇒ `true`（回第 1 页）：对标 `laydate` 的 `done`（L616-630）与工具栏
   *    `viewRecTotal`（L1255-1274）都是重调 `getRecOrderTotal()` —— 那是**重新 `table.render`**，
   *    页码天然回 1；
   *  - **改游戏 / 点卡片里的「刷新列表」** ⇒ `false`（**留在当前页**）：对标这两条走的是
   *    `reloadOrderList2()`（L2486-2496），它带 `page: { curr: page2 }`（`page2` 是 `done` 里记下的当前页）。
   *
   * ⚠️ 「不回第 1 页」**依赖后端「越界 `page` = 成功空页」**（Task 3 修复轮 2）：选了行集更小的
   * 游戏后原页码可能超末页，若后端回 `1002`，layui 会把它当失败处理。这条依赖写在这里，
   * 改后端那一支之前先看 `order_stats_views.py` 的 docstring 第 2 条。
   */
  function loadRecTotal(opts: { resetPage: boolean }): void {
    if (!recTotalRendered) {
      ;(table.render as (o: unknown) => void)({
        elem: `#${REC_TOTAL_TABLE_ID}`,
        url: ORDER_STATS_URL,
        method: 'get',
        // 本端点的分页参数名是 `page` / **`limit`**（`OrderStatsPagination.page_size_query_param`），
        // 与主表的 `page_size` 不同——写错的话页大小会静默回落成后端默认的 100。
        request: { pageName: 'page', limitName: 'limit' },
        where: recTotalWhere(),
        toolbar: '#toolbarDemo2',
        // 对标同对象里 `defaultToolbar` 写了两次（`[]` → `['filter','exports']`），**后写生效**
        // （`userlist.js` L2443/L2445）——右上角是「筛选列 + 导出」两个图标。
        defaultToolbar: ['filter', 'exports'],
        // ⚠️ 传**新数组**：layui 会往 `page` 上写 `curr`、删 `elem`/`jump`，
        //    直接传导出的常量会把常量改掉（同族踩坑见 `order-insiderecorderlist.ts`）。
        page: { limit: REC_TOTAL_DEFAULT_LIMIT, limits: [...REC_TOTAL_LIMITS] },
        cols: [recTotalCols()],
        text: { none: '暂无数据' },
        parseData: (res: {
          code: number
          msg: string
          data?: { items?: OrderStatsRow[]; total?: number }
        }) => ({
          code: res.code,
          msg: res.msg,
          count: res.data?.total ?? 0,
          data: res.data?.items ?? [],
        }),
        /**
         * ⚠️ **HTTP 层失败（401/403/5xx）必须把后端 `msg` 露出来**。
         * layui 的默认行为是往表里写一句它自己的「请求异常」+ 状态码文本 —— 403 那句
         * 「你没有查看接单统计的权限」会被吞掉。取 msg 用**既有**的 `backendErrorMessage`
         * （真实浏览器里第一参是 jqXHR、msg 在 `responseJSON.msg`，见该模块的实测注释），
         * 提示走本页统一的 `layer.msg`。
         *
         * ⚠️ **这里写表内空态是安全的，因为顺序是「`errorView` → 本回调」**（读码 vendored
         * `layui.js` 的表格 ajax `error:` 分支：`n.errorView(...)` 在前、`o.error && o.error(e,t)`
         * 在后），所以此刻 `.layui-none` 已经是最新那个节点。
         * ⛔ **不要**再加 `done` 回调去「补写」这段文案：HTTP 失败**不触发** `done`，
         * 那个补写只会留到**下一次成功**时把过期的错误话贴到「暂无数据」上（假消息）。
         * 业务码失败（`1000`/`1002`）也不走这里：那是 HTTP 200 + `code != 0`，layui 自己会把
         * `parseData` 出来的 `msg` 写进同一个空态。
         */
        error: (res: unknown): void => {
          const text = backendErrorMessage(res)
          const none = recTotalPanel()?.querySelector<HTMLElement>('.layui-none')
          if (none) none.textContent = text
          msg(text)
        },
      })
      recTotalRendered = true
      return
    }
    // 已渲染过 ⇒ 用 `reload`（别再 `render` 一次：那会重建表格视图并把页码打回 1）。
    const reloadOptions: Record<string, unknown> = { where: recTotalWhere() }
    if (opts.resetPage) reloadOptions.page = { curr: 1 }
    ;(table.reload as (id: string, o: unknown) => void)(REC_TOTAL_TABLE_ID, reloadOptions)
  }
  /* ──────────────────── 接单统计卡片结束 ──────────────────── */

  /**
   * 支付宝查重的**失败态文案**（`#tableList3` 的 `text.none`，见 `ALIPAY_CHECK_TEXT`）。
   *
   * ⚠️ 403 单独认：那个端点的权限节点是 `finance.payroll`，缺它就是这个状态——
   * 它**不是**报错，而是「你没权限看」，所以要给一句能懂的话，不能落进「没有重复」。
   * 其余失败（网络 / 500 / 401 等）用 `failed`，不把服务端的英文或技术细节抛给用户。
   */
  const alipayFailureText = (e: unknown): string =>
    e instanceof HttpError && e.status === 403 ? ALIPAY_CHECK_TEXT.forbidden : ALIPAY_CHECK_TEXT.failed

  /**
   * 取「重复收款支付宝」并渲染 `#tableList3`。
   *
   * 三种结局**互不相同**（E-0004）：
   * 1. 有重复 ⇒ `data` 是摊平后的行（见 `buildAlipayCheckRows`）；
   * 2. 查出来没有 ⇒ `data: []` + `none` =「没有重复的收款支付宝账号」；
   * 3. 失败 / 无权限 ⇒ `data: []` + `none` = 可读的失败提示（**不是**第 2 种那句）。
   *
   * ⚠️ 只有**一个** `table.render`，且发生在取数之后：本页的子表是 `data` 直给（没有 layui 的
   * `url`），先渲染一次空壳再渲染一次结果会在同一个 `elem` 上留下两层 `.layui-table-view`
   * （对标用 `url` 所以只 render 一次、由 layui 自己取数）。代价是卡片打开到数据回来之间
   * 表格区是空的——那段极短，且失败态也一定会渲染出来。
   * ⚠️ 端点**无参**、**不看列表筛选**（租户内全量），所以这里 `fetchAlipayDuplicates()` 直接调。
   */
  async function renderAlipayCheck(): Promise<void> {
    try {
      const result = await fetchAlipayDuplicates()
      renderSubTable({ ...alipayCheckTableOptions(), data: buildAlipayCheckRows(result) })
    } catch (e) {
      renderSubTable({ ...alipayCheckTableOptions(), data: [], text: { none: alipayFailureText(e) } })
    }
  }

  /**
   * 「删除用户」：**两次确认**都点掉之后才动手（对标 `userlist.js` 的 `delUserBtn`）。
   *
   * ⛔ 文案照抄对标（见 `DELETE_CONFIRM_TEXTS`），但**实际是软删、可恢复**：
   * 「彻底无法恢复」在当前实现下**不成立**。这条注释与那段常量一起看，别只读一半。
   *
   * ⚠️ 后端是**逐行**端点（`POST api/accounts/<id>/delete/`，无批量），所以这里对**勾选的每一行**
   * 逐个调；`Promise.all` 而不是串行会更贴对标，但逐行 `try` 才能给出「成功了几个」的汇总文案
   * （部分失败时用户要知道到底删了几个，不能只说「失败」）。
   */
  async function onDeleteSelected(): Promise<void> {
    const rows = requireChecked()
    if (!rows) return
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      DELETE_CONFIRM_TEXTS.first,
      { title: '删除用户' },
      (firstIndex: number) => {
        closeLayer(firstIndex)
        ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
          DELETE_CONFIRM_TEXTS.second,
          { title: '删除用户' },
          (secondIndex: number) => {
            closeLayer(secondIndex)
            void (async () => {
              let done = 0
              let failed = 0
              let firstError = ''
              for (const row of rows) {
                try {
                  await deleteAccount(row.id)
                  done += 1
                } catch (e) {
                  failed += 1
                  if (firstError === '') {
                    firstError = e instanceof Error ? e.message : '删除失败'
                  }
                }
              }
              msg(
                failed === 0
                  ? `已删除 ${done} 个账号`
                  : `已删除 ${done} 个，${failed} 个失败：${firstError}`,
              )
              reload()
            })()
          },
        )
      },
    )
  }

  /**
   * 「恢复」：把已软删的账号从回收站里放回去（**我方新增**，对标没有回收站）。
   *
   * ⚠️ **恢复 ≠ 启用**：后端不碰 `is_active` ⇒ 恢复后这人仍然登录不了（有意，见 `restoreAccount`）。
   * 刷新列表是必须的：默认（非「已删除」）口径下这一行恢复后就**不该再出现**了。
   */
  async function onRestore(id: number): Promise<void> {
    try {
      await restoreAccount(id)
      msg('已恢复')
    } catch (e) {
      msg(e instanceof Error ? e.message : '恢复失败')
    }
    reload()
  }

  async function onToggleStatus(): Promise<void> {
    const rows = requireChecked()
    if (!rows) return
    const allActive = rows.every((r) => r.is_active)
    try {
      await setAccountsStatus(
        rows.map((r) => r.id),
        !allActive,
      )
      msg(allActive ? '已停用' : '已启用')
      reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : '操作失败')
    }
  }

  async function onResetPassword(id: number): Promise<void> {
    ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
      '确定将该用户的登录密码重置为 123456 吗？',
      { title: '重置密码' },
      async (index: number) => {
        ;(layer.close as (i: number) => void)(index)
        try {
          await resetAccountPassword(id)
          msg('密码已重置为 123456')
        } catch (e) {
          msg(e instanceof Error ? e.message : '重置失败')
        }
      },
    )
  }

  const onToolbarClick = (event: Event): void => {
    // ⚠️ **子表**工具栏（`#toolbarDemo2`/`#toolbarDemo3` 渲染出来的那三颗按钮）先接住：
    // 它们**没有** `lay-event`（见 `subtableToolbarTemplate()`），只有 `data-sub`，
    // 点了统一提示「尚未开放」——本任务不接子表动作，下一个任务按 `data-sub` 的原值分派。
    // 判断放在 `[data-tb]` 之前：子表按钮不在主工具栏里，两个选择器不会同时命中。
    // 子表被 `layer.open` 搬进 `.layui-layer` 后父链仍是 `FORM.layui-form → root`，委托收得到。
    const sub = (event.target as HTMLElement).closest<HTMLElement>('[data-sub]')
    if (sub) {
      // 接单统计卡片（`#toolbarDemo2`）的「刷新列表」是**真功能**：卡片现在真取数了
      // （合并 main 带入的 P3e Task 4），对标这一颗走 `reloadOrderList2()` ⇒ **留在当前页**。
      // 支付宝卡片（`#toolbarDemo3`）的三颗、以及「禁用子账号 / 删除用户」仍提示尚未开放（D-0025）。
      if (sub.dataset.sub === 'refresh2' && sub.closest(REC_TOTAL_PANEL_SELECTOR)) {
        loadRecTotal({ resetPage: false })
        return
      }
      notOpen(sub.textContent?.trim() ?? '')
      return
    }
    const el = (event.target as HTMLElement).closest<HTMLElement>('[data-tb]')
    if (!el) return
    const action = el.dataset.tb
    const label = el.dataset.label ?? ''
    switch (action) {
      case 'new':
        // 对标 `userlist.js:1043-1045` 的 `case 'addUserBtn': addEditUser(0)` ⇒ 开的是
        // 「新开系统子账户」iframe 弹层（`pages/user-adduser.ts`），**不是**跳到 `/system-users/new`。
        // ⚠️ `/system-users/new` 那条**路由保留**（由菜单 / 直接访问进入，仍要能用）；
        // 这里换掉的只是工具栏这颗按钮的出口。详见 `mount` 的注释。
        ;(layer.open as (o: unknown) => void)({
          ...ADD_EDIT_USER_DIALOG_BASE,
          title: addEditUserDialogTitle(0),
          content: addEditUserDialogContent(0),
          end: () => {
            reload()
          },
        })
        break
      case 'refresh':
        reload()
        break
      case 'toggle':
        void onToggleStatus()
        break
      // ⚠️ 这里**没有** `case 'tag'`：工具栏「设置标记」那颗按钮已按业务方 2026-09-22 的裁剪删除
      //（它原先走 `patchSelected({tag})`）。「标记」的**页内**入口仍在——表格「标记」列那条链接
      // 由下面的 `onCellClick` 接住（`act === 'tag'`），业务方删的是按钮，不是这个功能。
      case 'games': {
        // 「设置游戏权限」= 对标那个**独立 iframe 页面**（`/User/SetGames?id=<uid>`），
        // 不再是自造的 `<select multiple>` 通用弹层。实参见 `SET_GAMES_DIALOG_BASE` /
        // `setGamesDialogContent`；子页保存成功后自己关掉弹层，`end` 在这里把列表刷新一遍。
        // ⚠️ `uid` 在**开弹层时**就要定下来（子页是独立请求，拿不到父页的勾选态）⇒ 先要勾选行。
        const rows = requireChecked()
        if (!rows) break
        ;(layer.open as (o: unknown) => void)({
          ...SET_GAMES_DIALOG_BASE,
          content: setGamesDialogContent(rows.map((r) => r.id)),
          end: () => {
            reload()
          },
        })
        break
      }
      // ⭐ 自有按钮「押金额度」（对标没有；2026-10-23 打手个人押金额度线）。
      // 一次只能配一个人（计划 §10 明确不做批量）⇒ 0 行 / 多行各给一句提示，都不开弹层。
      // 子页从 `window.location.search` 读 `uid`（`IslandContext` 不带 query 参数），
      // 保存成功后自己关弹层，`end` 在这里把列表刷新一遍（与「设置游戏权限」同一套）。
      case 'depositQuota': {
        const rows = requireChecked()
        if (!rows) break
        if (rows.length > 1) {
          msg('押金额度只能给一个人配置')
          break
        }
        ;(layer.open as (o: unknown) => void)({
          ...DEPOSIT_QUOTA_DIALOG_BASE,
          content: depositQuotaDialogContent(rows[0]!.id),
          end: () => {
            reload()
          },
        })
        break
      }
      case 'type':
        // 对标 `userlist.js:1097-1109` 的弹层实参（含 `content: $('.typebox')`）——
        // 不再是自造的 `.layer-form`。保存处理器在 `onSaveBusinessType`（绑 `.settypes`）。
        openTypeDialog()
        break
      case 'price':
        // 对标 `userlist.js:1136-1148` 的弹层实参（含 `content: $('.recPricebox')`）。
        // 保存处理器在 `onSaveRecPrice`（绑 `.setRecPriceSaveBtn`）。
        openRecPriceDialog()
        break
      case 'selfCount': {
        // 「设自助接单数」——对标 `userlist.js:2371-2408`（`setSelfRecOrderCount`）：
        // 一个 `layer.prompt`，填的是「内部接单页面接单上限」。
        // ⚠️ 落库口径由业务方裁定：**一个值同时写两个上限**
        // （`grab_limit_dl` 代练 / `grab_limit_pw` 陪玩）——所以载荷里**两个键都要有**
        // （只写一个是本轮变异证明要抓的错）。
        const rows = requireChecked()
        if (!rows) break
        ;(layer.prompt as (o: unknown, cb: (v: string, i: number) => void) => void)(
          // `value` 的预填规则见 `selfCountPromptValue`（有意偏离对标的写死 `value: 0`）。
          { ...SELF_COUNT_PROMPT_BASE, value: selfCountPromptValue(rows) },
          (value, index) => {
            // 校验只要求**非负整数**：`0` 是**合法值**（= 不限制），别把它当成「没填」。
            // 判据复用 `isPositiveInt`（`/^\d+$/`），比对标的 `VerifyInt`（`/^\+?[0-9]*$/`）
            // 紧两处——它接受空串与纯 `+`，会造成静默写 0 / 写 NaN（见 `isPositiveInt` 的注释）。
            if (!isPositiveInt(value)) {
              msg(DIALOG_MESSAGES.selfCountInvalid)
              return
            }
            const limit = Number(value)
            void patchSelected(
              { grab_limit_dl: limit, grab_limit_pw: limit },
              '接单上限已保存',
            ).then((ok) => {
              if (ok) closeLayer(index)
            })
          },
        )
        break
      }
      case 'settle':
        // 「禁/解结算」——对标 `userlist.js:1229-1241`（`setDisSettBtn` → `setUserDisSettSta`）：
        // **没有弹窗**，直接切换；`UpUserDisSettSta` 是**按 uid 逐个翻转**。
        // ⚠️ 所以我方是**逐行各自取反**（`!row.settle_forbidden`），**不是**「把所有行统一设成一个值」
        // ——混合勾选（有的被冻结、有的没有）时，统一置值会把一半行写成反的。
        // ⚠️ `undefined`（无档案行 / 老接口没带该键）按 **`false`（模型默认）** 归一：
        // `!== true` 翻转成 `true`，而不是把 `undefined` 发出去（E-0021）。
        void patchEachRow(
          (row) => ({ settle_forbidden: row.settle_forbidden !== true }),
          '已切换禁止结算状态',
        )
        break
      case 'recTotal':
        // 对标 `userlist.js:1255-1274`：开「接单统计」卡片 → 渲染 `#tableList2` → 渲染卡片
        // 里的三颗内嵌筛选（游戏权限下拉 + 接单开始日 + 接单结束日）。
        // ⚠️ 本分支原先这里是**空外壳**（`recTotalTableOptions()`）；合并裁定取 main 的**真取数**
        // （`GET /api/business/order-stats`，见上面的 `loadRecTotal`），外壳与弹层实参仍是本分支的。
        // 「打开即取数」（对标 `layer.open` 之后紧跟 `getRecOrderTotal()`）。
        openCard(CARD_TITLES.recTotal, '.recTotalDiv', () => {
          loadRecTotal({ resetPage: true })
          ;(form.render as (t?: string) => void)()
        })
        break
      case 'aliPay':
        // 对标 `userlist.js:1279-1312`：**先弹二级下拉菜单**，点了菜单项才开卡片。
        // `show: true` = 外部事件触发即显示；菜单只有一项（`id=10`），`click` 里判 id。
        ;(dropdown.render as (o: unknown) => void)({
          elem: '#checkDropdown',
          show: true,
          data: CHECK_DROPDOWN_MENU,
          shade: [0.3, '#2f363c'],
          click: (menudata: { id: number }) => {
            if (menudata.id !== 10) return
            // 对标 `getCheckIdenticalAliPayAcc()`：开卡片 + 取数（`table.render` 带 url）。
            // 我们走自己的 `fetchAlipayDuplicates()`（无参、不看列表筛选），取到才渲染。
            openCard(CARD_TITLES.aliPay, '.checkAliPayAccDiv', () => {
              void renderAlipayCheck()
            })
          },
        })
        break
      case 'delete':
        // ⭐ Task 8：「删除用户」从「点了提示未开放」变成**真动作**——两次确认 → 逐个软删。
        void onDeleteSelected()
        break
      default:
        notOpen(label)
    }
  }

  const onCellClick = (event: Event): void => {
    // ⛔ **判据是 `[data-act]` 本身，不是某个 class**（实测踩出来的）：
    // 原先这里写 `.user-cell-act, .editOrderBy`，而**操作列**那三颗锚点
    // （`data-act="edit"` / `"reset"` / `"restore"`，见 `buildCols` 的操作列 templet）
    // 复刻的是对标的 `layui-btn` 类、**没有** `user-cell-act`（那是我方内部钩子，对标没有）
    // ⇒ `closest()` 返回 `null`、这里提前 `return` ⇒ **三颗全部点不动**，
    // 底下的 `restore` / `edit` / `reset` 分支**都是死代码**。
    // ⚠️ 后果最重的是「恢复」：已删除的行**只渲染这一颗链接** ⇒ 恢复这条路在 UI 上**完全不可达**。
    // ⚠️ 它同样静默了很久：「编辑」「重置」也一直是死的（业务方 2026-09-23 报「没实现」，
    // 根因不是没写、而是**点不动**）。
    // 为什么放宽到 `[data-act]` 而不是给那三颗补 `user-cell-act` 类：
    //   ① `data-act` **就是**动作标记（模板逐处产出它），拿它当选择器是「判据打在真实出口上」；
    //   ② 补 class 会**改动渲染出来的 HTML**——操作列那两颗锚点的 `class` 是对标逐字复刻的
    //      （spec 另有对拍），而 `user-cell-act` 对标没有，塞进去等于把内部钩子混进复刻标记；
    //   ③ 一处覆盖**全部 7 个** `data-act` 产出点（edit/restore/reset/tag/games/orderby），
    //      不会再漏——这正是「维护一份平行的 class 清单」必然出错的地方。
    const el = (event.target as HTMLElement).closest<HTMLElement>('[data-act]')
    if (!el) return
    const act = el.dataset.act
    const id = Number(el.dataset.id ?? 0)
    if (act === 'tag') {
      // 当前值 = 该行渲染出来的标记（`drawCols` 里 `d.tag || '无'`）；空标记渲染成「无」，
      // 故把它折回空串。对标是从 `data-tag` 属性取（`userlist.js:1751`）——我们的标记列
      // 没有那个属性，用链接文案是同一份值。
      const current = el.textContent?.trim() === '无' ? '' : (el.textContent?.trim() ?? '')
      ;(layer.prompt as (o: unknown, cb: (v: string, i: number) => void) => void)(
        // 对标 `userlist.js:1755` 的实参（title/formType/maxlength 见 `TAG_PROMPT_BASE`）。
        { ...TAG_PROMPT_BASE, value: current },
        (value, index) => {
          // 对标 `userlist.js:1756-1759`：值没变 ⇒ 只提示「无需提交」，**不提交**。
          if (value === current) {
            msg(DIALOG_MESSAGES.tagUnchanged)
            return
          }
          void updateAccount(id, { tag: value }).then(
            () => {
              closeLayer(index)
              msg('标记已保存')
              reload()
            },
            (e: Error) => msg(e.message),
          )
        },
      )
    } else if (act === 'games') {
      // 对标 `userlist.js:1792-1805`（`.games` 点击 ⇒ `setUserGames(id)`）：开的是
      // 「设定游戏权限」弹层（与工具栏 `case 'games'` 同一颗），**不是**编辑页。
      // 旧实现跳编辑页是一个对标偏差（Task 13 修正）。
      ;(layer.open as (o: unknown) => void)({
        ...SET_GAMES_DIALOG_BASE,
        content: setGamesDialogContent([id]),
        end: () => {
          reload()
        },
      })
    } else if (act === 'orderby') {
      // 当前值就是链接文案（`buildCols` 的 templet 渲染的是 `d.order_by ?? 0`）。
      const current = el.textContent?.trim() ?? '0'
      ;(layer.prompt as (o: unknown, cb: (v: string, i: number) => void) => void)(
        // 对标 `userlist.js:2330` 的实参。
        { ...ORDER_BY_PROMPT_BASE, value: current },
        (value, index) => {
          // 对标 `userlist.js:2331-2334`：值没变 ⇒ 直接关层、**不提交**（与原值相同的 PATCH 没有意义）。
          if (value === current) {
            closeLayer(index)
            return
          }
          // 对标 `userlist.js:2335-2338`：不是正整数 ⇒ 这句，并且**不关层**（让用户改）。
          // 判据见 `isPositiveInt`（比对标的 `VerifyInt` 紧，理由写在那里）。
          if (!isPositiveInt(value)) {
            msg(DIALOG_MESSAGES.orderByInvalid)
            return
          }
          void updateAccount(id, { order_by: Number(value) }).then(
            () => {
              closeLayer(index)
              msg('排序值已保存')
              reload()
            },
            (e: Error) => msg(e.message),
          )
        },
      )
    } else if (act === 'edit') {
      // 对标 `userlist.js:1340-1344`（`obj.event === 'submitEdit'` ⇒ `addEditUser(obj.data.uid)`）⇒
      // 「编辑系统子账户」iframe 弹层。**一个分支盖住两个入口**：操作列那颗「编辑」与
      // 登录账号 / 账号昵称两列由 `accountLink()` 产出的账号链接（见 `buildCols`）。
      ;(layer.open as (o: unknown) => void)({
        ...ADD_EDIT_USER_DIALOG_BASE,
        title: addEditUserDialogTitle(id),
        content: addEditUserDialogContent(id),
        end: () => {
          reload()
        },
      })
    } else if (act === 'restore') {
      // ⭐ Task 8：回收站里的那一行只有这一颗链接（见 `buildCols` 的操作列 templet）。
      void onRestore(id)
    } else if (act === 'reset') {
      void onResetPassword(id)
    }
  }

  // ⚠️ 页内那颗 `onNotOpenClick`（按 `[data-notopen]` 提示「尚未开放」）**已随本任务删除**：
  // `.typebox` / `.recPricebox` 的两颗「保存」现在有真的处理器（`onDialogSaveClick`），
  // 全页再没有 `data-notopen`（判据见 spec 的「整页已无 `data-notopen`」）。
  root.addEventListener('click', onToolbarClick)
  root.addEventListener('click', onCellClick)
  root.addEventListener('click', onDialogSaveClick)

  // 身份切换条：点 tab = 设 `userType` 下拉值 → 按同一份 `buildUserListWhere`
  // 重取（与手改下拉同一参数出口）；高亮只由 tab 点击驱动（同步是单向的，见上方 docstring）。
  root.addEventListener('click', (event) => {
    const el = (event.target as HTMLElement | null)?.closest?.(
      `.${IDENTITY_TAB_CLASS}`,
    ) as HTMLElement | null
    if (el === null) return
    const key = el.getAttribute(IDENTITY_TAB_ATTR) ?? ''
    const tab = IDENTITY_TABS.find((t) => t.key === key)
    if (tab === undefined) return
    ;(form.val as (f: string, o: Record<string, string>) => void)('sform', {
      userType: tab.userType,
      studioFlag: tab.studioFlag,
      workerFlag: tab.workerFlag,
    })
    const values = (form.val as (f: string) => Record<string, string>)('sform')
    reload({ where: buildUserListWhere(values) })
    root.querySelectorAll(`.${IDENTITY_TAB_CLASS}`).forEach((chip) => {
      chip.classList.remove(IDENTITY_TAB_ACTIVE_CLASS)
    })
    el.classList.add(IDENTITY_TAB_ACTIVE_CLASS)
  })

  // 表单：搜索 + 开关
  ;(form.on as (e: string, cb: (obj: { field?: Record<string, string>; elem?: HTMLInputElement }) => void) => void)(
    'submit(seachBtn)',
    () => {
      // 「哪些筛选还没做」那份忽略清单（`NOT_OPEN_FILTERS`）与「本次已忽略」提示已随
      // Task 3 一起删除：筛选栏 10 个控件现在**全部**接上了后端，没有需要忽略的值。
      const values = (form.val as (f: string) => Record<string, string>)('sform')
      ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
        where: buildUserListWhere(values),
        page: { curr: 1 },
      })
    },
  )
  ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
    `switch(staChecked)`,
    (obj) => {
      const id = Number(obj.elem?.dataset.id ?? 0)
      // ⚠️ 新状态取 `elem.checked`，**不要**用 `obj.value`：layui 派发的 `value` 是 input 的
      // **value 属性**（vendored `layui.js` 原文 `{elem:n[0],value:n[0].value,othis:i}`），
      // 而 `switchCell` 不写 `value` ⇒ checkbox 默认值恒为 `"on"` ⇒ 拿它判布尔会**恒真**：
      // 点「禁用」实际发的是「启用」，还提示「已保存」、账号停不掉（E-0071）。
      // 对标 `userlist.js:835`（`staChecked`）/ `:980` 用的也是 `obj.elem.checked`（2026-09-23 当场 grep 复核）。
      const next = obj.elem?.checked === true
      void setAccountStatus(id, next).then(
        () => msg('已保存'),
        (e: Error) => {
          msg(e.message)
          reload()
        },
      )
    },
  )
  /**
   * 开关列的 PATCH 载荷。**穷尽 switch**（三条约束一起满足，⛔ 别再退化成三元）：
   * ① **显式分支**而不是 `{ [field]: next }`：计算属性名的键在 TS 下会被推成 `string` 索引，
   *    而这个载荷必须是 `AccountWritePayload` 的合法子集。
   * ② **穷尽**：`default` 里那句 `const never: never = field` 让「往 `REAL_SWITCHES` 加第三个开关、
   *    却忘了在这里加分支」变成 **`vue-tsc` 红**（类型层就断，不用等运行期）。
   * ③ ⚠️ 原先是三元 `field === 'settle_forbidden' ? … : { grab_forbid_self_service }`——它把
   *    「**不是**第一个」当成「就是第二个」，**加第三个开关时会静默写到 `grab_forbid_self_service` 上**
   *    （`E-0021` 同族：判据比被判定的事实窄，且全绿时看不出来）。整体评审 Minor 14，2026-09-23 收口。
   */
  const switchPatch = (
    field: (typeof REAL_SWITCHES)[number]['field'],
    next: boolean,
  ): Partial<AccountWritePayload> => {
    switch (field) {
      case 'settle_forbidden':
        return { settle_forbidden: next }
      case 'grab_forbid_self_service':
        return { grab_forbid_self_service: next }
      default: {
        // 穷尽性检查（见上②）：走到这里 `field` 必须是 `never`，否则 `vue-tsc` 会红。
        const never: never = field
        return never
      }
    }
  }

  /**
   * 两个**真开关列**的处理器：PATCH 该行的真实布尔值。
   *
   * ⚠️ **新状态读 `obj.elem.checked`，不是 `obj.value`**——这是本页最容易写错的一处：
   * vendored layui 的 `form` 在 change 时发的是 `{elem, value: elem.value, othis}`
   * （`public/layui/layui.js` 的 `syncAppearanceOnPropChanged` 附近那段），而 `elem.value`
   * 是**输入框的 `value` 属性**：我们的开关标记没有 `value` 属性 ⇒ HTML 默认 `"on"`，
   * **开关打开/关闭它都不变**。拿它当状态会恒判为「开」。对标自己也是读 `obj.elem.checked`
   * （`userlist.js:979-980` 的 `form.on('switch(isProhibitPublicRecOrder)')`）。
   * ⛔ **成功也必须 `reload()`**（T7 评审移交的 **Important 2**，别改回去）：不刷新的话
   * layui 的**行缓存**（`table.checkStatus` 就是从 vendored `layui.js` 的 `F.cache` 建的）
   * 还是**旧值**，而「禁/解结算」是按缓存值**取反**的 ⇒ 两步操作
   * 「先拨这个开关、再对同一行点『禁/解结算』」会把**开关之前的旧值取反**又发一遍
   * ——等于把当前值重发一次，还弹「已切换」：**看起来成功、实际什么都没变**。
   * ⚠️ 选 `reload()` 而不是「把 PATCH 的响应并回行缓存」：后者要伸进 layui 的私有
   * `F.cache`（版本相关，换 layui 版本就静默失效），而 `reload()` 是本页其余写路径
   * （标记 / 排序值 / 状态 / 结算）已经在用的公共路径，口径一致。
   * 判据是**两步**用例（`system-user-list.interaction.spec.ts` 的
   * 「拨开关 → 再点禁/解结算」）——单步用例抓不到这条（删掉 `reload()` 它照样绿）。
   */
  for (const sw of REAL_SWITCHES) {
    ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
      `switch(${sw.filter})`,
      (obj) => {
        const id = Number(obj.elem?.dataset.id ?? 0)
        const next = obj.elem?.checked === true
        void updateAccount(id, switchPatch(sw.field, next)).then(
          () => {
            msg('已保存')
            reload()
          },
          (e: Error) => {
            msg(e.message)
            reload()
          },
        )
      },
    )
  }

  /**
   * ⭐ 自有列「个人押金额度」的切换（2026-10-23 追加）：**乐观更新 + 失败回滚**。
   *
   * 乐观更新 = layui 已经把开关画成新状态了，我们**不等后端**就让它先显示着；
   * 失败时把 `elem.checked` 拨回切换前的值、再 `form.render('checkbox')` 让 layui 按
   * 输入框的真值重画那颗 switch，随后弹后端 `msg`（⛔ 不翻译、不减字）。
   *
   * 为什么**不**像上面两个真开关那样在失败时 `reload()`：
   * ① `reload()` 会**回第 1 页**（本页 `reload()` 固定带 `page: { curr: 1 }`）——在第 3 页
   *    拨错一个开关就把用户甩回第 1 页，代价远大于「回滚这一格」；
   * ② 这个开关**不参与任何按缓存取反的动作**（不像「禁/解结算」读 `table.checkStatus`），
   *    所以不需要为了行缓存一致性整表重取。
   * 成功时也**不** `reload()`：服务端已经接受，行缓存里那一列不是任何动作的输入；
   * 下次翻页 / 点刷新自然从后端拿真值。
   *
   * 只在 `showDepositQuota` 时注册处理器：没有这一列的人**连处理器都不该有**
   * （fail-closed；否则是一条没有任何 UI 出口、只能被合成事件触发的死写入路径）。
   * 判据：`system-user-list.interaction.spec.ts` 的「拨开关 ⇒ PUT 只带 enabled」
   * 与「失败 ⇒ 拨回去 + 弹后端 msg」两条。
   */
  if (showDepositQuota) {
    ;(form.on as (e: string, cb: (obj: { elem?: HTMLInputElement }) => void) => void)(
      `switch(${DEPOSIT_QUOTA_COLUMN.filter})`,
      (obj) => {
        const elem = obj.elem
        const id = Number(elem?.dataset.id ?? 0)
        const next = elem?.checked === true
        // 回滚的落点 = 切换**前**的状态（layui 已经先把 UI 翻过来了）。
        const previous = !next
        void setDepositQuotaEnabled(id, next).then(
          () => {
            msg('已保存')
          },
          (e: Error) => {
            if (elem) {
              elem.checked = previous
              // layui 的 switch 是渲染出来的壳：只改 `checked` 不重画，界面会停在错的状态。
              ;(form.render as (t?: string) => void)('checkbox')
            }
            msg(e.message)
          },
        )
      },
    )
  }
  // 卡片里的「游戏权限」下拉：选游戏 ⇒ 重取、**照对标不回第 1 页**（`select(selectGameTotal)` →
  // `reloadOrderList2()`）。它筛的是**名单**（后端按「这个人的游戏权限」筛行集合，数值不变）。
  ;(form.on as (e: string, cb: (obj: { value?: string }) => void) => void)(
    `select(${REC_TOTAL_GAME_FILTER})`,
    () => {
      loadRecTotal({ resetPage: false })
    },
  )

  // 默认日期窗 = 今天往前 3 个自然月 ~ 今天（对标那两个值是服务端渲染的；我们只能挂载时算）。
  // ⚠️ 算它是**纯函数**（`defaultRecTotalRange`，含月末取最后一天的边界），单测钉着；
  //    别把这段逻辑塞进下面 `done` 回调里。
  const defaultRange = defaultRecTotalRange()
  const startInput = document.getElementById(REC_TOTAL_START_ID) as HTMLInputElement | null
  const endInput = document.getElementById(REC_TOTAL_END_ID) as HTMLInputElement | null
  if (startInput) startInput.value = defaultRange.stime
  if (endInput) endInput.value = defaultRange.etime
  // 对标 `laydate.render({elem:'#rsDate', type:'date'})` ×2（`userlist.js` L616-630，只有 `elem`+`type`）：
  // 默认值不来自 laydate，来自上面那两行。改日期 ⇒ 重取并**回第 1 页**（对标那里是重新 render）。
  ;(laydate.render as (o: unknown) => void)({
    elem: `#${REC_TOTAL_START_ID}`,
    type: 'date',
    done: () => loadRecTotal({ resetPage: true }),
  })
  ;(laydate.render as (o: unknown) => void)({
    elem: `#${REC_TOTAL_END_ID}`,
    type: 'date',
    done: () => loadRecTotal({ resetPage: true }),
  })

  // 表：单元格编辑（保证金）+ 渲染完成后的表单/统计
  ;(table.on as (e: string, cb: (obj: { data: AccountRow; value: string; field: string }) => void) => void)(
    `edit(${TABLE_FILTER})`,
    (obj) => {
      if (obj.field !== 'deposit_amount') return
      void updateAccount(obj.data.id, { deposit_amount: obj.value === '' ? 0 : obj.value }).then(
        () => msg('保证金已保存'),
        (e: Error) => msg(e.message),
      )
    },
  )

  ;(table.render as (o: unknown) => void)({
    elem: `#${TABLE_ID}`,
    url: ACCOUNTS_URL,
    method: 'get',
    toolbar: '#userToolbarTpl',
    // 对标主表的 `defaultToolbar` 是 `['filter']`（= 表格右上角的「筛选列」图标），
    // 取自 `userlist.js` 里 `table.render` 的生效值（同对象写两次，后写生效）。
    // 我们此前写 `[]`，于是那个图标一直缺着——parity 当时也不比这块（已要求工具补上）。
    defaultToolbar: ['filter'],
    request: { pageName: 'page', limitName: 'page_size' },
    parseData: (res: {
      code: number
      msg: string
      data?: { items?: AccountRow[]; total?: number }
    }) => ({
      code: res.code,
      msg: res.msg,
      count: res.data?.total ?? 0,
      data: res.data?.items ?? [],
    }),
    // `limits` 逐字取对标 `userlist.js` L815 的 `limits: [10, 20, 30, 40, 50, 100]`（6 项）。
    // 少了 100 会让「条/页」下拉少一项、宽度也差 7px——探针的 option 数差异第一次就报在这里。
    page: { limit: 20, limits: [10, 20, 30, 40, 50, 100] },
    // ⭐ 自有列「个人押金额度」**只给持节点的人**：`showDepositQuota` 与工具栏那颗按钮同源
    //    （同一次 `viewerCanAdjustTakerWallet()`），默认 `false` ⇒ 没权限的人看到的列集合不变。
    cols: [buildCols(gameNames, showDepositQuota)],
    text: { none: '暂无数据' },
    done: (res: { count: number }) => {
      ;(form.render as (t?: string) => void)()
      // 统计文案的落点是对标工具栏模板里的 `#total`（在按钮下方、表格上方）。
      const totalEl = document.getElementById('total')
      if (!totalEl) return
      if (res.count > 200) {
        // 我们一次最多只取 200 行来数「正常/禁用/保证金」，超过就只报总数
        // （对标那三个数是服务端聚合字段，我们没有对应接口）。
        totalEl.textContent = `本系统内共有${res.count}个系统子账户`
        return
      }
      void listAccounts({ page: 1, page_size: 200 }).then((all) => {
        const active = all.items.filter((r) => r.is_active).length
        const deposit = all.items.reduce((s, r) => s + Number(r.deposit_amount ?? 0), 0)
        // ⚠️ 模板**逐字**取自对标 `userlist.js` L821-822（i18n 字典同文，见同文件 L322-324）：
        //   '本系统内共有' + count + '个系统子账户，其中有' + (count-disableCount) + '个系统子账户状态正常，'
        //   + disableCount + '个系统子账户被禁用，总计已登记保证金' + totalInsureAmount + '元'
        // 三个要点：**逗号前没有空格**、**「共有」后没有空格**、保证金是**服务端原样数字**（`0元` 不是 `0.00 元`）。
        // 上一版写成「共有 N 个…，其中有 N 个…，总计已登记保证金 0.00 元」，探针逐字对不上。
        const depositText = Number.isInteger(deposit) ? String(deposit) : deposit.toFixed(2)
        totalEl.textContent = `本系统内共有${res.count}个系统子账户，其中有${active}个系统子账户状态正常，${all.items.length - active}个系统子账户被禁用，总计已登记保证金${depositText}元`
      })
    },
  })

  return () => {
    root.removeEventListener('click', onToolbarClick)
    root.removeEventListener('click', onCellClick)
    root.removeEventListener('click', onDialogSaveClick)
    root.innerHTML = ''
  }
}

/**
 * 标准 island 页出口（`layui-island/page.ts` 的注册表按它调用）。
 *
 * ⚠️ Task 13 之后本页**不再**用 `ctx.onNavigate` 做「新增 / 编辑」跳转：那两颗出口改成
 * `layer.open({type:2})` 打开 `pages/user-adduser.ts`（子页关掉自己后由 `end` 刷新列表）。
 * `/system-users/new` 与 `/system-users/:id/edit` 两条**路由保留**（直接访问仍要能用，
 * 由 Vue 壳承载），只是不再从本页跳过去。`ctx.onNavigate` 仍原样往下传（见
 * `mountUserListPage` 的 `_options`）——签名是对外契约，且将来可能再需要页内跳转。
 */
export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountUserListPage(root, { onNavigate: ctx.onNavigate })
}

/** 注册表按 `import.meta.glob('./pages/*.ts')` 的**默认导出**取页面（见 `layui-island/page.ts`）。 */
export default { mount } satisfies IslandPage
