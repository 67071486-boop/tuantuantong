/**
 * 【系统人员】的声明式描述：账号表单用到的枚举，与**已上线**的「人员管理」页
 * （`src/layui-island/pages/system-user-list.ts`）消费的工具栏/筛选/列配置。
 *
 * ⚠️ 7 个 Element Plus 占位页（页面组件 + 其筛选/列 schema 表 + `PlaceholderSchema` 类型）
 * 已按 D-0026 换成 layui island 1:1 复刻页并从本文件删除（`router/index.ts` 的 7 条
 * `islandPage` 路由）。本文件剩下的导出都只服务于**已上线**的人员管理页，别顺手删。
 *
 * 结构来源（**实测/读码**，快照不进 git）：
 * `.tools/recon/sysuser/STRUCTURE-EVIDENCE.md`（鸡蛋铺 9 页逐页证据）。
 * 本文件只做「**结构与内容对齐**」：列标题、筛选标签、按钮文案逐条照抄对标物。
 *
 * ⚠️ 对标缺陷**不复刻**（证据文档 §10.3）：`ApplyOnboardingMange` 空 `<title>`、
 * `BackUserList` 的 `lastTime`/`createTime` 列错配、`UserContractSigning` 操作列复用
 * `sigingCompleteTime`、`GetSystemContarctList` 拼写、`isDisSett` 撞名导致组群筛选不可达、
 * 实名设置弹窗无读取端点等。
 */

/* ── 账号表单/列表用到的枚举（镜像 `apps/accounts/choices.py`，后端是唯一出处） ── */

export const BUSINESS_TYPE_OPTIONS = [
  { label: '代练+陪玩', value: 0 },
  { label: '专做代练', value: 1 },
  { label: '专做陪玩', value: 2 },
] as const

export const MEMBER_TYPE_OPTIONS = [
  { label: '内部成员', value: 0 },
  { label: '合作用户', value: 1 },
] as const

export const COMMISSION_MODE_OPTIONS = [
  { label: '固定金额', value: 10 },
  { label: '订单价百分比', value: 20 },
  { label: '利润的百分比', value: 30 },
] as const

/** 提成标签随模式切换（对标物 `.tcLabel` 的行为，读码）。 */
export function commissionValueLabel(mode: number): string {
  return mode === 10 ? '固定提成金额' : '百分比数值'
}

/**
 * 人员管理工具栏 **14 个按钮**，按对标物 `#toolbarDemo` 的**原文顺序**逐条照抄（证据文档 §2.5）。
 *
 * ⚠️ 对标是 **21 颗**；业务方 2026-09-22 裁掉其中 7 颗（设置标记 / 禁止看订单密码 /
 * 允许看订单密码 / 禁止看订单电话 / 允许看订单电话 / 设置排序 / 组建群聊），
 * 留下的 14 颗**顺序不变**（不是重排，是中间抽掉 7 颗）。本页从此不再 1:1——业务方的决定。
 * ⚠️ 「设置标记」的**页内功能**没删：表格「标记」列那条链接照样能点（`onCellClick` 的 `act === 'tag'`）。
 *
 * `action` 决定点了做什么：接了我方后端的走对应动作；没有后端的统一 `notOpen`
 * （**照渲染、点了提示「尚未开放」**，不再置灰）。
 * `needsSelection` 为真的按钮在没有勾选行时禁用（避免点了没反应）。
 * `type` 是**对标物按钮的配色**（layui 的 `layui-btn-danger`/`normal`/默认绿/`warm`），
 * 映射到 Element Plus 的 `danger`/`primary`/`success`/`warning`——见业务方给的实测截图。
 */
export type UserToolbarAction =
  | 'new'
  | 'refresh'
  | 'toggle'
  | 'tag'
  | 'games'
  // ⭐ **自有**按钮「押金额度」的动作。
  // ⚠️ 它**不在** `USER_TOOLBAR` 的 14 颗对标按钮里（对标没有这个入口）——见
  // `layui-island/pages/system-user-list.ts` 的 `DEPOSIT_QUOTA_BUTTON`。
  | 'depositQuota'
  | 'type'
  | 'price'
  | 'selfCount'
  | 'settle'
  | 'recTotal'
  | 'aliPay'
  // ⭐ Task 8：「删除用户」从 `notOpen` 改成真动作（**软删**，见 `system-user-list.ts` 的
  // `case 'delete'`）。⚠️ 它**不新增**权限节点：后端挂在 `system_user.change_status` 上。
  | 'delete'
  | 'notOpen'

export type UserToolbarType = 'primary' | 'success' | 'warning' | 'danger'

export type UserToolbarItem = {
  label: string
  action: UserToolbarAction
  /** 对标物上这颗按钮的颜色。 */
  type: UserToolbarType
  /** 需要先勾选账号才能用。 */
  needsSelection?: boolean
}

export const USER_TOOLBAR: UserToolbarItem[] = [
  // 配色逐条取自对标 `#toolbarDemo` 每个 `<button>` 的 class（实测）：
  // danger=红、`layui-btn-normal`=蓝、`layui-btn-warm`=橙、无颜色类=默认绿。
  { label: '新增系统子账户', action: 'new', type: 'danger' },
  { label: '刷新列表', action: 'refresh', type: 'success' },
  { label: '禁用/启用', action: 'toggle', type: 'warning', needsSelection: true },
  { label: '发起/更改签约', action: 'notOpen', type: 'primary' },
  { label: '设置游戏权限', action: 'games', type: 'success', needsSelection: true },
  { label: '设置业务类型', action: 'type', type: 'success', needsSelection: true },
  { label: '设自助接单数', action: 'selfCount', type: 'success', needsSelection: true },
  { label: '设抢单价范围', action: 'price', type: 'success', needsSelection: true },
  { label: '禁/解结算', action: 'settle', type: 'success', needsSelection: true },
  { label: '统计完单率', action: 'notOpen', type: 'success' },
  { label: '接单统计', action: 'recTotal', type: 'success' },
  { label: '检测功能', action: 'aliPay', type: 'success' },
  { label: '加入黑名单', action: 'notOpen', type: 'success' },
  // ⭐ Task 8：这颗按钮**真的能用**了（软删 + 两次确认）。文案/配色/顺序都没动
  // （`spec` 的「不漂移」对拍仍逐字相等），变的只有 `action`。
  { label: '删除用户', action: 'delete', type: 'warning' },
]

/**
 * 对标主表里 `hide:true` 的 **6 列**（证据文档 §2.3 第 3/5/9/10/23/25 行）。
 * 它们**默认不显示**，由工具栏右侧那个「列显示」控件勾选后才出现——与对标物一致
 * （第一版把它们全摊开是错的：截图里根本看不到这几列）。
 */
export const OPTIONAL_USER_COLUMNS = [
  { key: 'realName', label: '姓名' },
  { key: 'sign', label: '签约' },
  { key: 'completeRate', label: '完单率' },
  { key: 'priceLimit', label: '接单价限制' },
  { key: 'lastLogin', label: '最后登录' },
  { key: 'creator', label: '开户人' },
] as const

/** 两张子表（接单统计 / 支付宝重复检测）的工具栏按钮，同样「点了提示未开放」。 */
export const NOT_OPEN_SUBTABLE_TOOLBAR = ['刷新列表', '禁用子账号', '删除用户'] as const

/**
 * 人员管理表格里**真能改**的两个开关列（其余 4 个开关列已按业务方 2026-09-22 的裁剪删除）。
 * 宽度照对标 `userlist.js` 的 `isProhibitSettStatus=40` / `isProhibitPublicRecOrder=80`。
 * `field` = 后端字段名（PATCH `/api/accounts/<pk>/` 的键，也是列表行数据里的键）；
 * `filter` = layui `form.on('switch(…)')` 的过滤器名（我方命名，勿与页面别处重名）。
 *
 * 任务沿革：Task 1 只保留列（渲染写死 `notOpenChecked`，点了提示尚未开放）；
 * **Task 7 起两列一起接线**——`checked` 取该行真实布尔值，`filter` 就是下面这两个。
 *
 * ⚠️ `grab_forbid_self_service` 由**另一条在飞的线**加到后端（本分支切出时尚未在 `main`）；
 * 这里只写前端引用，TS 类型上按**可选**处理（见 `api/accounts.ts` 的 `AccountProfile`）。
 *
 * ## 出处（2026-09-23 合并 `main` 时从 `main` 的同功能实现搬过来的证据）
 *
 * 列定义与模板在**两份快照里都有**；行号取自**目标租户**证据件 `.tools/recon/suqing1/`
 * （业务方指定的权威租户，84 页当场重扒、不复用旧租户产物，见
 * `docs/FRONTEND-REPLICA-STANDARD.md` §10.1「证据必须落在目标租户上」与 `docs/ERRORS.md` 的 E-0020）：
 * - 「禁自助接单」`.tools/recon/suqing1/assets/JS2019/User/userlist.js:772` 的列定义
 *   （`field: 'isProhibitPublicRecOrder'` … `width: 80`）；模板 `#switchTpl6` 在
 *   `.tools/recon/suqing1/html/User__userlist.html:261-263`。
 * - 「禁止结算」`.tools/recon/suqing1/assets/JS2019/User/userlist.js:763` 的列定义
 *   （`field: 'isProhibitSettStatus'` … `width: 40`）；模板 `#switchTpl8` 在
 *   `.tools/recon/suqing1/html/User__userlist.html:267-269`。
 * - ⚠️ **如实边界**：`.tools/recon/sysuser/` 是**更早租户**的产物（本页/本文件其余处仍引它），
 *   `#switchTpl6` 在那边是 **266–268** 行；两份 `userlist.js` 实测**逐字节相同**（`cmp` 退出 0），
 *   两份 HTML 行数不同（431 vs 441）。⇒ 上面的行号只在**本注释指定的那一份**上成立；
 *   「哪一份权威」按 §10.1 由目标租户决定，此处按 suqing1 取号——**这是推断边界，不是对
 *   `sysuser/` 的否定**（仓内多处文档仍以它为该页快照，那些引用不改）。
 *
 * ## 两条**与对标不同、已知且已登记**的偏离（2026-09-23 合并 `main` 时裁定）
 *
 * `main` 上另有一条线独立接了同样两个开关，它把对标的两个属性**逐字照抄**：
 * 列头 `title` 带 `<span lay-tips="…">`、`filter` 用对标的 `disSettStatus` /
 * `isProhibitPublicRecOrder`。本线**不采用**那两处，用自己的一套：
 * - `title` 用**纯标签**（不带 `lay-tips` span）：该属性在我们 vendored 的 layui 里是**死属性**
 *   （`grep -c lay-tips` = 0），加不加**视觉上完全一样**；本线早已把它登记为「已接受的偏离」。
 * - `filter` 用我方事件名（`settleForbiddenChecked` / `grabForbidChecked`）：它只是
 *   列与处理器之间的**内部约定**，与页面别处不重名即可，不影响渲染。
 * ⚠️ **这条分歧已如实登记**（归整体评审分诊），不是静默取舍：`main` 那版**更贴近对标原文的 HTML**。
 *
 * ⚠️ 两处**字段名与对标不一样是刻意的**：对标的 `field` 是 `isProhibitSettStatus` /
 * `isProhibitPublicRecOrder`，我方接口字段是 `settle_forbidden` / `grab_forbid_self_service`
 * （`StaffProfile` 的列名）——照抄对标的 `field` 会让列表行取不到值，开关初值恒为「否」。
 */
export const REAL_SWITCHES = [
  { label: '禁止结算', field: 'settle_forbidden', width: 40, filter: 'settleForbiddenChecked' },
  { label: '禁自助接单', field: 'grab_forbid_self_service', width: 80, filter: 'grabForbidChecked' },
] as const

/**
 * **已接后端**的真开关列「禁自助接单」的声明式定义（`PATCH /api/accounts/<id>/`）。
 *
 * 出处（逐字照抄，**2026-09-23 当场 `grep -n` 复核过行号**——行号是移动靶）：
 * 列定义与模板在**两份快照里都有**；本注释的行号取自**目标租户**证据件
 * `.tools/recon/suqing1/`（业务方指定的权威租户，84 页当场重扒、不复用旧租户产物，
 * 见 `docs/FRONTEND-REPLICA-STANDARD.md` §10.1「证据必须落在目标租户上」与 `docs/ERRORS.md` 的 E-0020）：
 * `.tools/recon/suqing1/assets/JS2019/User/userlist.js:772` 的列定义
 * `field: 'isProhibitPublicRecOrder', title: '<span lay-tips="激活成绿色代表…">禁自助接单</span>',
 * width: 80, align: 'center', templet: '#switchTpl6'`，与 `#switchTpl6` 模板
 * （`.tools/recon/suqing1/html/User__userlist.html:261-263`：
 * `lay-skin="switch" lay-text="是|否" lay-filter="isProhibitPublicRecOrder"`）。
 * ⚠️ **如实边界**：`.tools/recon/sysuser/` 是**更早租户**的产物（本页/本文件其余处仍引它），
 * 该模板在那边是 **266–268** 行；两份 `userlist.js` 本轮实测**逐字节相同**（`cmp` 退出 0），
 * 两份 HTML 行数不同（431 vs 441）。⇒ 上面两个行号只在**本注释指定的那一份**上成立；
 * 「哪一份权威」按 §10.1 由目标租户决定，此处按 suqing1 取号——**这是推断边界，不是对
 * `sysuser/` 的否定**（仓内多处文档仍以它为该页快照，那些引用不改）。
 *
 * ⚠️ 两处**字段名不一样是刻意的**：对标的 `field` 是 `isProhibitPublicRecOrder`，
 * 我方接口字段是 `grab_forbid_self_service`（`StaffProfile` 的列名）——照抄对标的 `field`
 * 会让 `d.grab_forbid_self_service` 取不到值，开关初值恒为「否」。
 * `filter` 则**照抄对标**（事件名与数据字段无关）。
 *
 * `title` 里的 `<span lay-tips="…">` 是**逐字复刻**（同 `user-userloginrestrict.ts` 的先例；
 * 原引用 `system-smssetting.ts` 已随 2026-09-23 短信整块下线删除）：
 * `lay-tips` 在我们 vendored 的 layui 里是死属性（`grep -c lay-tips` = 0），不渲染任何东西。
 */
export const GRAB_FORBID_SWITCH = {
  /** 我方接口字段（`apps/accounts/payloads.py::_profile_fields`）。 */
  field: 'grab_forbid_self_service',
  label: '禁自助接单',
  /** 列头 HTML：对标的 `title` 原文（含 `lay-tips`）。 */
  title:
    '<span lay-tips="激活成绿色代表这个人在内部抢单页面无法接单，如果你想让这个人不能在内部抢单页面接单，就激活成绿色">禁自助接单</span>',
  /** 对标 `userlist.js:772` 的 `width` 原值。 */
  width: 80,
  /** 对标 `switchTpl6` 的 `lay-filter`（= 页面上 `form.on('switch(…)')` 的事件名）。 */
  filter: 'isProhibitPublicRecOrder',
  /** 对标 `switchTpl6` 的 `lay-text`（`是`=开/禁止，`否`=关）。 */
  text: '是|否',
} as const

/**
 * **已接后端**的真开关列「禁止结算」的声明式定义（`PATCH /api/accounts/<id>/`）。
 *
 * 出处（逐字照抄，**2026-09-23 当场 `grep -n` 复核过行号**——行号是移动靶）：
 * 列定义 `.tools/recon/suqing1/assets/JS2019/User/userlist.js:763` 原文
 * `field: 'isProhibitSettStatus', title: '<span lay-tips="如果你临时不想给TA结算，可以开启此开关，此人在冻结期间的所有账单都将无法结算，其它功能不受影响">禁止结算</span>', width: 40, align: 'center', unresize: false, templet: '#switchTpl8'`；
 * 模板 `.tools/recon/suqing1/html/User__userlist.html:267-269` 的 `#switchTpl8`：
 * `lay-skin="switch" lay-text="是|否" lay-filter="disSettStatus"`（初值 `d.isProhibitSettStatus == 'Y'`）。
 * `lay-tips` 整句与 tips 字典 `userlist.js:300`（`isProhibitSettStatus: '禁止结算'`）一致；
 * 同文件 `:299` 的 `isProhibitSettStatustips` 就是这一整句。
 *
 * ⚠️ 与「禁自助接单」同形，两处**字段名不一样是刻意的**：对标的 `field` 是 `isProhibitSettStatus`，
 * 我方接口字段是 `settle_forbidden`（`StaffProfile` 的列名，Task 2 交付）——照抄对标的 `field`
 * 会让 `d.settle_forbidden` 取不到值，开关初值恒为「否」。`filter` 则**照抄对标**
 * （事件名与数据字段无关），即 `#switchTpl8` 的 `disSettStatus`。
 */
export const SETTLE_FORBID_SWITCH = {
  /** 我方接口字段（`apps/accounts/payloads.py::_profile_fields`）。 */
  field: 'settle_forbidden',
  label: '禁止结算',
  /** 列头 HTML：对标的 `title` 原文（含 `lay-tips` 整句）。 */
  title:
    '<span lay-tips="如果你临时不想给TA结算，可以开启此开关，此人在冻结期间的所有账单都将无法结算，其它功能不受影响">禁止结算</span>',
  /** 对标 `userlist.js:763` 的 `width` 原值（全表最窄的开关列）。 */
  width: 40,
  /** 对标 `switchTpl8` 的 `lay-filter`（= 页面上 `form.on('switch(…)')` 的事件名）。 */
  filter: 'disSettStatus',
  /** 对标 `switchTpl8` 的 `lay-text`（`是`=开/禁止，`否`=关）。 */
  text: '是|否',
} as const

/* ── 人员管理筛选栏的静态选项（逐条照抄证据文档 §2.2 的 option 文案） ── */

export const USER_STATUS_FILTER_OPTIONS = [
  { label: '全部', value: '' },
  { label: '有效', value: 'true' },
  { label: '禁用', value: 'false' },
  // ⭐ Task 8：第 4 项「已删除」（**放在最后**）。⚠️ 它的 `value` 是字符串 `'deleted'`
  // **不是布尔** ⇒ `submit(seachBtn)` 里要分支处理：`'deleted'` 走 `where.deleted = true`，
  // 其余 true/false 仍走 `where.is_active`（旧的 `is_active` 语义没被抢）。
  // ⚠️ 它**不改变**筛选栏的控件数（仍是同一个「有效状态」下拉，16 个 `.layui-input-inline` 不变）。
  { label: '已删除', value: 'deleted' },
] as const

// 「全部」的取值**有意偏离对标原文**：对标（`User__userlist.html`）的 `bussType`「全部」是
// `0`，而 `0` 是 `business_type` 的真实枚举值（`BusinessType.BOTH` = 代练+陪玩）——
// 于是「全部」把「代练+陪玩」整个吃掉，**UI 上永远筛不了它**。业务方裁定
// 「『业务类型』这个筛选要修复」⇒ 本页「全部」改用一个**不与真实枚举冲突的哨兵** `-1`，
// 把 `0` 交还给「代练+陪玩」。
// ⚠️ **为什么不是空串**（T3b 第一版就是空串，被真浏览器实测推翻）：这个下拉由 vendored 的
//    `formselects-v4` 渲染（`public/vendor/formselects-v4.js`），而它对空串取值**不友好**：
//    ① L733-735 把「**第一个** option 且 `value` 为假」的那一项**整个跳过**——即空串的「全部」
//       根本**不会渲染成一个可点选项**；
//    ② L168 于是把它的文案当作**占位提示**（灰字），不是选中项；
//    ③ L264 初始选中项**只**来自 `option[selected]`，没有该属性就一项都不选。
//    后果：用户选过「代练」之后，下拉里**没有「全部」可点**，回不到全部。
//    实测原始输出见 `.superpowers/sdd/2026-09-22-system-user-list-completion/task-4-report.md`「改动 0」。
// ⚠️ 顺序照 **D-0086**（专做代练 → 专做陪玩 → 代练+陪玩），与「新开系统账号」页一致。
// ⚠️ 与 `MEMBER_TYPE_FILTER_OPTIONS` 的「全部」= `-1` **同一约定**：`-1` 不是合法枚举值，
//    不像 `0` 那样与真实值相撞。
export const BUSINESS_TYPE_FILTER_OPTIONS = [
  { label: '全部', value: -1 },
  { label: '代练', value: 1 },
  { label: '陪玩', value: 2 },
  { label: '代练+陪玩', value: 0 },
] as const

export const MEMBER_TYPE_FILTER_OPTIONS = [
  { label: '全部', value: -1 },
  { label: '内部成员', value: 0 },
  { label: '合作用户', value: 1 },
] as const

export const CERT_FILTER_OPTIONS = [
  { label: '全部', value: '' },
  { label: '未实名', value: 'N' },
  { label: '已实名', value: 'Y' },
] as const

export const SETTLE_FILTER_OPTIONS = [
  { label: '全部', value: '' },
  { label: '未禁止', value: 'N' },
  { label: '已禁止', value: 'Y' },
] as const

/** 「业类」列的徽标文案（对标物：代陪 / 代练 / 陪玩）。 */
export const BUSINESS_TYPE_LABELS: Record<number, string> = { 0: '代陪', 1: '代练', 2: '陪玩' }

/** 「账号昵称」列的前后缀徽标（对标物：外 / 内）。 */
export const MEMBER_TYPE_BADGE: Record<number, string> = { 0: '内', 1: '外' }
