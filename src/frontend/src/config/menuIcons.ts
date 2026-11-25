import type { GlyphIconName } from './layuiIconNames'

/** 可见菜单单元的图标。**推断级**（对标物每个条目用的图标未采到，见 FRONTEND-THEME §5.2）。
 *
 *  换图标**要动两处**（不是「只改本文件」）：
 *   1. 改下面的映射；
 *   2. 确认新名字在 `src/styles/layui-icons.css` 里**已有一条 `.layui-icon-<名字>::before` 规则**，
 *      没有就照着那个文件的头注补一条（码点从 layui 官方 CSS 抄）。
 *  只做第 1 步而漏了第 2 步，屏幕上会是空白/豆腐块，而**类型检查与名字注册表都不会红**
 *  （新名字在 196 个里存在，只是我们没有它的字形）——拦这件事的是
 *  `__tests__/menuIcons.spec.ts` 里读 `layui-icons.css` 的那三条测试。
 *
 *  类型用 `Record<string, …>` 是为了让调用方 `GROUP_ICONS[g.name]`（key 是 `string`）可查；
 *  键的约束交给后面的 `satisfies`：**键打错字或漏了某个可见分组都会编译不过**，
 *  不必等到运行时测试（类型是第一道防线）。
 *  值用 `GlyphIconName`（不是 `LayuiIconName`）：它已把「注册表里有名字、但没有字体字形」
 *  的那类（码点 `null`，如 `indeterminate`）排除，所以映射表里写不出这种名字。 */

/** 可见分组的标签，与 `menu.ts` 的分组名逐字一致（漂移由 `menuIcons.spec.ts` 的键校验兜住）；
 * 「功能维护」是唯一的例外——它不是 `menu.ts` 的组名，而是【第三方平台】在 **v2 侧栏**的
 * 渲染层改名（`v2Menu.V2_GROUP_LABEL_OVERRIDES`），同名键校验对它按覆盖表放行。 */
type VisibleGroupLabel =
  | '我的相关' | '订单管理' | '系统人员' | '财务相关' | '游戏维护' | '系统维护'
  // ⭐ 【发平台设置】从整组隐藏改为可见（组内只放出「交易平台对接设置」）。
  | '发平台设置'
  // ⭐【客户维护】2026-11-10 业务方拍板整组放出（客户线）。
  | '客户维护'
  // 【第三方平台】是对标物里没有的一组（业务方指令新增），见 `menu.ts` 的组注释。
  | '第三方平台'
  // 【功能维护】=【第三方平台】的 v2 侧栏渲染层改名（2026-11-06 第三方平台批；
  // `config/menu.ts` 零改动、旧壳照旧显「第三方平台」），见下面 GROUP_ICONS 的别名行。
  | '功能维护'
  // 【开放平台】同上（业务方指令新增的一组，里面是自有功能页「性能分析」）。
  | '开放平台'
  // 【APP与小程序】同上（业务方指令新增的一组，里面是自有功能页「数据概览」）。
  | 'APP与小程序'
/** 可见的顶部直挂条目标签，同上。 */
type VisibleTopLabel = '后台首页' | '指派给我的订单' | '内部抢单大厅'
/**
 * **侧栏最底部**直挂条目的标签（`menu.ts` 的 `BOTTOM_ITEMS`，与上面两张表并列的第三条）。
 *
 * ⚠️ 它与 `VisibleTopLabel` **分开声明**（不是并进那一张）：两者渲染在侧栏的不同位置
 * （`AppSidebar.vue` 里一个在顶部直挂区、一个在 `el-sub-menu` 之后），
 * 合并成一张表会让「底部条目漏了图标」与「顶部条目漏了图标」互相冒充。
 */
type VisibleBottomLabel = '问题反馈'

export const GROUP_ICONS: Record<string, GlyphIconName> = {
  我的相关: 'username',
  订单管理: 'form',
  系统人员: 'group',
  财务相关: 'rmb',
  游戏维护: 'app',
  系统维护: 'set',
  // ⭐ 【发平台设置】放出后必须有图标。⚠️ 同上**复用 `app`**（字形集合只有 14 个）。
  发平台设置: 'app',
  // ⭐【客户维护】2026-11-10 客户线放出。⚠️ **复用 `service`**（客服耳机，与顶部「内部抢单大厅」
  // 同一个字形）：现有 17 个字形里没有「客户」专用图标，按本仓「新增只能复用」的先例选语义最近的。
  客户维护: 'service',
  // ⚠️ **复用 `app`**（与【游戏维护】同一个字形）：`layui-icons.css` 里只有 14 个字形，
  // 而 `menuIcons.spec.ts` 要求「用到的名字集合 == 有规则的名字集合」⇒ 新增分组**只能**复用。
  // 换图标要动两处（映射 + `src/styles/layui-icons.css` 的码点），见本文件头。
  第三方平台: 'app',
  // ⭐【功能维护】=【第三方平台】在 v2 侧栏的渲染层改名（`v2Menu.V2_GROUP_LABEL_OVERRIDES`，
  // 2026-11-06 第三方平台批）。⚠️ 本行是**显式豁免**「config 零改动」的别名增量（业务方在
  // 「改名后期望侧回查旧组名取图标」与「别名行」二选一中拍板后者）：CarbonShell 按
  // `GROUP_ICONS[改名后的组名]` 取图标，没有这行 v2 侧栏该组图标会静默消失。
  // ⚠️ **复用 `app`**（与上一行同一个字形，理由逐字相同）。
  功能维护: 'app',
  // ⚠️ 同上**复用 `app`**（理由与代价与【第三方平台】那一行逐字相同）。
  开放平台: 'app',
  // ⚠️ 同上**复用 `app`**（2026-10-23【APP与小程序】；字形集合只有 14 个，新增分组只能复用）。
  APP与小程序: 'app',
} satisfies Record<VisibleGroupLabel, GlyphIconName>

export const TOP_LEVEL_ICONS: Record<string, GlyphIconName> = {
  后台首页: 'home',
  指派给我的订单: 'list',
  内部抢单大厅: 'service',
} satisfies Record<VisibleTopLabel, GlyphIconName>

/**
 * **侧栏最底部**那一条的图标（业务方指令新增的「问题反馈」，
 * 见 `menu.ts` 的 `BOTTOM_ITEMS`）。
 *
 * ⚠️ **复用 `form`**（与【订单管理】分组同一个字形）：`src/styles/layui-icons.css` 里只有
 * 14 个字形，而 `menuIcons.spec.ts` 有一条**双向**集合断言
 * （「用到的名字集合 == 有规则的名字集合」，多写一条规则也算失败）⇒ 新增条目**只能**复用。
 * 选 `form` 是因为它在本批里表示「填一张表 / 提一条东西」，与反馈页的语义最近
 * （这是**我们的判断**，不是从对标产物取的值——本条目是对标物里没有的自有功能，
 * ⛔ 没有可对标的选择依据）。换图标要动两处（本映射 + `layui-icons.css` 的码点），见本文件头。
 */
export const BOTTOM_ICONS: Record<string, GlyphIconName> = {
  问题反馈: 'form',
} satisfies Record<VisibleBottomLabel, GlyphIconName>

/**
 * 非菜单用途的字形。与上面两张菜单映射分开，是因为消费者不同（页签条的刷新按钮 vs 侧栏条目），
 * 但**同一个字形系统**：两者都由 AppIcon 渲染、都要在 `src/styles/layui-icons.css` 里有
 * `.layui-icon-<名字>::before` 规则，也都被 `config/__tests__/menuIcons.spec.ts` 一起校验
 * （该文件的 `MAPPED` 展开本表，双向集合相等与码点比对两条断言随之覆盖它）。
 */
export const UI_ICONS = {
  refresh: 'refresh-1',
} as const satisfies Record<string, GlyphIconName>

/**
 * 按钮上的动作图标（与菜单无关，但**同一个字形系统**：都由 AppIcon 渲染、都要在
 * `src/styles/layui-icons.css` 里有 `.layui-icon-<名字>::before` 规则、都被
 * `config/__tests__/menuIcons.spec.ts` 的 `MAPPED` 一起校验）。
 *
 * 消费者：游戏维护三页的工具栏与操作列（spec §3；对标原文的按钮图标是 layuiAdmin 的私有图标
 * `layui-jia` / `layui-shanchu`，那几个**没取到码点**，所以新增用它官方对应的 `add-1`——已登记为偏差）。
 */
export const ACTION_ICONS = {
  // ⚠️ 键名刻意**不叫** `refresh`：`MAPPED` 是这几张表按 key 展开的普通对象，**同名键会静默合并**——
  // `UI_ICONS.refresh` 是 `refresh-1`，撞名会把它的字形从审计集合里挤掉（2026-09-20 实际撞过一次，
  // `menuIcons.spec.ts` 的「集合完全相等」当场红了）。所以这里用 `refreshPage` 区分「页签条刷新」与
  // 「列表工具栏刷新」两个不同的字形。
  refreshPage: 'refresh',
  remove: 'delete',
  // ⭐ v2 前端动作图标（D-0354/合并轮 2026-11-03）：my-orders 截图列「传/看」、录单成功大浮层
  // 对勾。v2 组件直接用 `layui-icon layui-icon-<名>` class（不经 AppIcon），但字形规则同样
  // 落在 src/styles/layui-icons.css、码点同样取自 layuiIconNames 生成表 ⇒ 照实登记进本表，
  // 让 menuIcons.spec 的双向集合与码点审计一并覆盖。
  shotUpload: 'upload',
  shotView: 'picture',
  saveOk: 'ok',
  edit: 'edit',
  add: 'add-1',
} as const satisfies Record<string, GlyphIconName>
