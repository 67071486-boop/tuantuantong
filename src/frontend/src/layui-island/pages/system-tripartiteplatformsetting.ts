/**
 * 【设置第三方交易平台接】页（island key = `system-tripartiteplatformsetting`）——
 * 证据件标记 + **真 API**（计划 T3/T4）。
 *
 * 形状（逐字来自 `public/replica/system-tripartiteplatformsetting.html`）：引文块 + 单表 +
 * 隐藏的「新增 / 修改」弹层 + 一整块「对所有平台生效」的 9 组单选。
 *
 * ## 为什么不再用 `artifactPage`
 *
 * 旧实现走 `artifactPage`：表格 `data: []` 写死、捕获阶段的「尚未开放」总闸把工具栏 / 行内 /
 * 开关的点击全吞掉（`src/layui/artifactPage.ts` 的 catch-all）⇒ 接真数据后整张表仍然恒空、
 * 按钮永远只弹提示。本页改成**自挂载**：`mountConfigLike`（取证据件 → 注入 → `table.render`
 * 一次 → `form.render` → 进 `onReady`），在 `onReady` 里自己挂 `table.on` / `form.on`、
 * 自己开弹层、自己保存。
 *
 * ## 与对标的差异（每条都给理由；值纪律见 `AGENTS.md` §5 / D-0025）
 *
 * | # | 差异 | 理由 |
 * |---|---|---|
 * | 1 | 表格列挂 **适配字段**（`platformName` / `status` / `isDefault` …） | 证据件的开关模板（`#switchTpl1-5`）读的是对标自己的字段名与**反向编码**（`status == 0` 才启用）；我们把 API 行映射成模板认得的形状，模板本身**一个字没改** |
 * | 2 | 「支付密码」列改成**函数 templet**（对勾 / 「点击设置」） | 对标本来就是函数模板（`tripartiteplatformsetting.js:76-84`）；旧实现把它抄成普通字段列 ⇒ 真数据下永远空白。现在由 `has_password` 驱动（后端只回布尔，不回密码） |
 * | 3 | 平台名后面多一枚「暂未开通」徽标（`is_available=false` 的行） | 业务方裁定：7 个平台都能存，但只有代练通有真客户端。徽标只**告知**，⛔ 不拦保存 |
 * | 4 | AppSecret 容器选择器用 `.platappsecretdiv1` | 证据件 HTML 是 `class="platappsecretdiv1"`，而对标 JS 找 `.platappsecretdiv`（少个 `1`）⇒ 对标自己那一句**永远匹配不到**、AppSecret 行靠 `display:none` 停着。我们按**标记的实际类名**取节点（修好，不是改证据件） |
 * | 5 | 「平台登录密码」那一格照渲染，但**不保存**，并写明尚未开放 | 我方模型没有这一列（接口也不收）。⛔ 不假装成功：格子下面直接写「尚未开放」，保存时若填了值再明确说「未提交」 |
 * | 6 | 保存成功后的提示是本仓文案（「已保存」+ 该平台暂未开通 / 未提交的说明） | 对标弹的是**它服务端**的 `data.msg`；本仓统一请求通道成功只回 `data`（`api/unwrap.ts`），拿不到那句 ⇒ 不自造一句谎称"已生效"（标准 §5） |
 * | 7 | 全局 9 组设置**逐组标注**「已生效 / 仅保存」：只有第 6 条（平台接手后自动发群二维码）已生效，其余 8 条仍只存不生效 | 业务方 2026-10-27（计划 §3 的生效范围总表）。上一版整块写「本版仅保存」已不成立；不分区标注客服会误判（F6） |
 *
 * ## 凭据红线（`AGENTS.md` §4）
 *
 * - `app_secret` 永不回显：弹层密码框**永远留空**，placeholder 写「留空 = 不改」；
 *   列表里**没有** AppSecret 这一列（计划 §1 第 2 条：列表只显示 AppId），也不显示 `has_secret`。
 * - 支付密码同理：列上只显示「有没有配」，点开用 `layer.prompt` 收新值，`PATCH` 后立即清屏。
 * - 测试夹具与代码里**没有**任何真实凭据（全部是 `synthetic-*` 形状的合成值）。
 *
 * ## 三条从对标抄下来的取值（别按直觉改）
 *
 * 1. 平台 radio **7 个，顺序照证据件**（10 / 30 / 80 / 90 / 93 / 50 / 60，代练通第一）；
 *    `openmodifydivview` 的分支只对这 7 个里有意义的那几个（`tripartiteplatformsetting.js:233-328`）。
 * 2. `0/1` 那 4 组全局设置里 `0` 有时是「否」、有时是「不限制 / 不发送」、`SK159` 的 `0`
 *    反而是「检测」⇒ ⛔ 不要写成 `Boolean(0)`。
 * 3. 对标在弹层里对 AppId 有一道前端必填闸（`platforms` 为 50 / 93 时放行，其余必填），
 *    本页照抄；真正的兜底仍是后端。
 */
import {
  createPlatformIntegration,
  deletePlatformIntegration,
  fetchPlatformIntegrations,
  fetchPlatformPushSettings,
  savePlatformPushSettings,
  updatePlatformIntegration,
  type PlatformIntegrationPayload,
  type PlatformIntegrationRow,
  type PlatformPushSettings,
} from '@/api/platformIntegrations'
import { findPanel, mountConfigLike, openPanelLayer } from '@/layui-island/config-replica'
import { escapeHtml } from '@/layui-island/escape'
import { islandMsg, type IslandPage } from '@/layui/island'

/** 证据件名（= island key = `pages/<key>.ts` 的文件名，`mountConfigLike` 按它取 HTML）。 */
export const PAGE_KEY = 'system-tripartiteplatformsetting'
/** 对标服务端渲染的 `<title>`（`System__tripartiteplatformsetting.html` L5；island 外壳全站共用一个静态 title）。 */
export const TITLE = '设置第三方交易平台接'

/** 表格 elem（对标 `#tableList`）与 `reloadData` / `table.on` 要的 **id**（不带 `#`）。 */
export const TABLE_ELEM = '#tableList'
export const TABLE_ID = 'tableList'

/** 弹层容器：证据件里**只有 class、没有 id**（`.modifyParamDiv`）⇒ `findPanel` 必须显式传它。 */
export const PANEL_CLASS = 'modifyParamDiv'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 「支付密码」那一格的类名（拼进 templet 标记）与选择器（查点击目标）——两者不能混（E-0061 ①）。 */
export const PAY_PASSWORD_TRIGGER_CLASS = 'modityPlatmPayPwd'
export const PAY_PASSWORD_TRIGGER_SELECTOR = `.${PAY_PASSWORD_TRIGGER_CLASS}`

/** 全局设置的保存按钮 `lay-filter`（证据件 html:244）。 */
export const SETTINGS_SAVE_FILTER = 'saveBtn1'
/** 页面上的「生效范围」声明（F6；业务方 2026-10-27 计划 §3 那张表）。 */
export const SETTINGS_NOTICE_ID = 'platformPushSettingsOnlySaveNotice'
/** 生效范围总声明：整块里**第 4 条与第 6 条**已生效，其余 7 条仍只存。 */
export const SETTINGS_NOTICE_TEXT =
  '本版有两条已生效：「撞单自动 0 撤」与「平台接手后自动发群二维码」；其余开关仍只保存、不影响业务'
/** 每组前面的生效范围标注（F6）：绿色 = 发单 / 同步链路真的读它；橙色 = 只存不生效。 */
export const SETTINGS_EFFECT_ON_TEXT = '已生效'
export const SETTINGS_EFFECT_OFF_TEXT = '仅保存'
export const SETTINGS_EFFECT_ON_CLASS = 'tt-settings-effect-on'
export const SETTINGS_EFFECT_OFF_CLASS = 'tt-settings-effect-off'
export const SETTINGS_EFFECT_ATTR = 'data-settings-effect'
/** 图例（插在总声明下面，逐条说明两个标注的含义）。 */
export const SETTINGS_EFFECT_LEGEND =
  '绿色「已生效」= 发单 / 同步链路会读它；橙色「仅保存」= 只写进系统，业务行为不变。逐组见下方标注。'

/**
 * 证据件 html:73-81 的**7 个平台 radio**（顺序逐字）；代练通 = 10。
 *
 * ⚠️ 不要放开成 `config/platforms.ts` 的 13 个：多出来的 6 个页面选不出来，
 * 后端接口也按同一份白名单拒绝（`AVAILABLE_PLATFORMS`）。
 */
export const PLATFORM_OPTIONS: ReadonlyArray<{ value: number; title: string }> = [
  { value: 10, title: '代练通' },
  { value: 30, title: '代练妈妈' },
  { value: 80, title: '代练丸子' },
  { value: 90, title: '趣代练' },
  { value: 93, title: '天玩代练' },
  { value: 50, title: '咕噜代练' },
  { value: 60, title: '易代练' },
]

/**
 * 今天**真有客户端**的平台（其余 6 个只落配置）。与后端
 * `platform_integration_views.PLATFORMS_WITH_CLIENT` 同源（后端也在响应里回 `is_available`，
 * 页面直接用那个布尔，这里只给"没有行时"的文案兜底用）。
 */
export const AVAILABLE_PLATFORMS: readonly number[] = [10]

/** 对标在弹层里**不要求 AppId** 的平台（`tripartiteplatformsetting.js:344` 的例外集，页面内的部分）。 */
const APPID_OPTIONAL_PLATFORMS = new Set([50, 93])

/** 证据件里那几格的 class 与选择器（取值一律来自证据件，不猜）。 */
export const ACCOUNT_LABEL_CLASS = 'platacclabel'
export const APP_ID_LABEL_CLASS = 'platappidlabel'
export const APP_SECRET_LABEL_CLASS = 'platappsecretlabel'
export const LOGIN_PASSWORD_DIV_CLASS = 'platloginpwddiv'
export const APP_ID_DIV_CLASS = 'platappiddiv'
export const PAY_PASSWORD_DIV_CLASS = 'platpaypwddiv'
/**
 * ⚠️ 证据件的类名就是 `platappsecretdiv1`（带一个 `1`），而对标 JS 找的是
 * `.platappsecretdiv`（`tripartiteplatformsetting.js:241` 等）——那是**对标的 bug**：
 * 它那一句永远匹配不到，AppSecret 行靠 HTML 的 `style="display:none"` 停着。
 * 本页按**标记的实际类名**取节点（修好，不改证据件）。
 */
export const APP_SECRET_DIV_CLASS = 'platappsecretdiv1'
export const REGISTER_DIV_CLASS = 'pt0'

const SEL = {
  accountLabel: `.${ACCOUNT_LABEL_CLASS}`,
  appIdLabel: `.${APP_ID_LABEL_CLASS}`,
  appSecretLabel: `.${APP_SECRET_LABEL_CLASS}`,
  loginPasswordDiv: `.${LOGIN_PASSWORD_DIV_CLASS}`,
  appIdDiv: `.${APP_ID_DIV_CLASS}`,
  payPasswordDiv: `.${PAY_PASSWORD_DIV_CLASS}`,
  appSecretDiv: `.${APP_SECRET_DIV_CLASS}`,
  registerDiv: `.${REGISTER_DIV_CLASS}`,
} as const

/** 「平台登录密码」尚未开放的说明（插在那格下面，随格子一起显隐）。 */
export const LOGIN_PASSWORD_NOT_OPEN_CLASS = 'platLoginPasswordNotOpen'
const LOGIN_PASSWORD_NOT_OPEN_SELECTOR = `.${LOGIN_PASSWORD_NOT_OPEN_CLASS}`
export const LOGIN_PASSWORD_NOT_OPEN_TEXT = '「平台登录密码」尚未开放：本版不会保存这一格的内容。'

/** 弹层里那一套「选平台后改 label / 显隐」的结果（纯值，便于测试）。 */
export type PlatformFieldConfig = {
  accountLabel: string
  appIdLabel: string
  appSecretLabel: string
  showLoginPassword: boolean
  showAppId: boolean
  showPayPassword: boolean
  showAppSecret: boolean
}

/**
 * 照 `tripartiteplatformsetting.js` 的 `resetaddmodifyform()`（默认值）与
 * `openmodifydivview()` 的**生效分支**（L233-328）算出本页 7 个平台的字段形态。
 *
 * ⚠️ `platform === 0` 是「还没选平台」（新增弹层的初始态）：沿用证据件 HTML 的初始状态
 * ——AppSecret 行是 `display:none`，平台账号 label 用 `resetaddmodifyform()` 的「平台用户ID」。
 * ⚠️ 对标里 `80` 另有一支分支（L286-292），但它在 L249 的 `30 || 80 || 90` 分支**之后**、
 * 永远到不了（死代码）⇒ 这里取**先命中的那一支**（80 与 30/90 同形）。⛔ 不复刻死代码。
 */
export function platformFieldConfig(platform: number): PlatformFieldConfig {
  const base: PlatformFieldConfig = {
    accountLabel: '平台用户ID',
    appIdLabel: 'AppId',
    appSecretLabel: 'AppSecret',
    showLoginPassword: false,
    showAppId: true,
    showPayPassword: true,
    showAppSecret: platform !== 0,
  }
  if (platform === 0) return base
  if (platform === 10) {
    return { ...base, accountLabel: '代练通用户ID' }
  }
  if (platform === 30 || platform === 80 || platform === 90) {
    return {
      ...base,
      accountLabel: '平台用户ID',
      appIdLabel: '商户标识',
      appSecretLabel: '商户密钥',
    }
  }
  if (platform === 50) {
    return {
      ...base,
      accountLabel: '平台账号',
      appSecretLabel: '对接秘钥',
      showLoginPassword: true,
      showAppId: false,
    }
  }
  if (platform === 93) {
    return {
      ...base,
      accountLabel: 'User_Code',
      appSecretLabel: '对接密钥',
      showAppId: true,
    }
  }
  // 60（易代练）与 7 个里剩下的：只要求 AppSecret（对标 L267-276 的 `60 || 100` 支）。
  return base
}

/** 把 `PlatformFieldConfig` 应用到弹层节点（先算值、再改 DOM；测试直接驱动它）。 */
export function applyPlatformFields(panel: HTMLElement, platform: number): void {
  const config = platformFieldConfig(platform)
  setText(panel, SEL.accountLabel, config.accountLabel)
  setText(panel, SEL.appIdLabel, config.appIdLabel)
  setText(panel, SEL.appSecretLabel, config.appSecretLabel)
  toggleVisible(panel, SEL.loginPasswordDiv, config.showLoginPassword)
  toggleVisible(panel, SEL.appIdDiv, config.showAppId)
  toggleVisible(panel, SEL.payPasswordDiv, config.showPayPassword)
  toggleVisible(panel, SEL.appSecretDiv, config.showAppSecret)
  // 「没账号？点这里注册」那颗只有平台 8 会显示（`openmodifydivview` 的 8 分支）；
  // 本页 7 个平台都不显示（证据件初始就是 display:none）。
  toggleVisible(panel, SEL.registerDiv, false)
}

function setText(panel: HTMLElement, selector: string, text: string): void {
  const el = panel.querySelector<HTMLElement>(selector)
  if (el) el.textContent = text
}

function toggleVisible(panel: HTMLElement, selector: string, show: boolean): void {
  const el = panel.querySelector<HTMLElement>(selector)
  if (el) el.style.display = show ? '' : 'none'
}

/** 在「平台登录密码」那一格下面补一句「尚未开放」（幂等；格子隐藏时它也一起隐藏）。 */
export function ensureLoginPasswordNotice(panel: HTMLElement): void {
  const div = panel.querySelector<HTMLElement>(SEL.loginPasswordDiv)
  if (!div || div.querySelector(LOGIN_PASSWORD_NOT_OPEN_SELECTOR)) return
  const note = document.createElement('div')
  note.className = `${LOGIN_PASSWORD_NOT_OPEN_CLASS} layui-form-mid layui-word-aux`
  note.textContent = LOGIN_PASSWORD_NOT_OPEN_TEXT
  div.appendChild(note)
}

/** 表格里的一行：接口字段（spread 保留）+ 对标 `cols[].field` / 开关模板认得的名字。 */
export type IntegrationTableRow = PlatformIntegrationRow & {
  /** 对标列 `platformName`（值取后端 `platform_label`，前端不维护第二份码表）。 */
  platformName: string
  /** 对标列 `platformAcc`。 */
  platformAcc: string
  /** 对标列 `appId`。 */
  appId: string
  /** 对标列 `concactQQ`。 */
  concactQQ: string
  /** 对标列 `concactPhone`。 */
  concactPhone: string
  /** `#switchTpl1`：对标 `status == 0` 才 checked ⇒ 0 = 启用。 */
  status: 0 | 1
  /** `#switchTpl5`：对标 `isDefault == 'Y'` 才 checked。 */
  isDefault: 'Y' | 'N'
  /** `#switchTpl2`：对标 `isAutoAccep == 0` 才 checked ⇒ 0 = 同步验收。 */
  isAutoAccep: 0 | 1
  /** `#switchTpl3`：对标 `isNotifyPushMan == 1` 才 checked。 */
  isNotifyPushMan: 0 | 1
  /** `#switchTpl4`：对标 `isAutoSelect == 0` 才 checked ⇒ 0 = 发单包含。 */
  isAutoSelect: 0 | 1
}

/** API 行 → 表格行（**只做名字与编码方向的对齐**，不改任何语义）。 */
export function integrationToTableRow(row: PlatformIntegrationRow): IntegrationTableRow {
  return {
    ...row,
    platformName: row.platform_label,
    platformAcc: row.account,
    appId: row.app_id,
    concactQQ: row.contact_qq,
    concactPhone: row.contact_phone,
    status: row.is_enabled ? 0 : 1,
    isDefault: row.is_default ? 'Y' : 'N',
    isAutoAccep: row.sync_accept ? 0 : 1,
    isNotifyPushMan: row.sms_notify ? 1 : 0,
    isAutoSelect: row.include_push ? 0 : 1,
  }
}

/**
 * 「平台名称」那一格（**纯函数**）：本版没有客户端的平台后面挂一枚「暂未开通」徽标。
 *
 * ⚠️ 只是告知——`is_available=false` 的**照常可以保存**（计划 §1 第 4 条 / 业务方裁定），
 * ⛔ 不要在这里做任何拦截。
 */
export function platformNameCell(row: PlatformIntegrationRow): string {
  const name = escapeHtml(row.platform_label || String(row.platform))
  if (row.is_available) return name
  return (
    `${name} <span class="layui-badge layui-bg-orange"` +
    ' lay-tips="该平台本版暂未开通：参数可以保存，但不会用于发单">暂未开通</span>'
  )
}

/**
 * 「支付密码」那一格的**函数模板**（逐字照 `tripartiteplatformsetting.js:76-84`）。
 *
 * - 已设置：绿对勾（`layui-icon-ok-circle`，色 `#16b777`，24px）+ 点击可改；
 * - 未设置：红徽标「点击设置」。
 *
 * ⚠️ 只读 `has_password`（后端只回布尔，明文永不回显）；`data-id` 供点击时定位那一行。
 */
export function payPasswordCell(row: PlatformIntegrationRow): string {
  if (row.has_password) {
    return (
      `<span class="layui-icon layui-icon-ok-circle ${PAY_PASSWORD_TRIGGER_CLASS}"` +
      ' style="color:#16b777;font-size:24px"' +
      ` data-id="${row.id}" lay-tips="已设置平台支付密码，点击可以更改"></span>`
    )
  }
  return (
    `<span class="layui-badge ${PAY_PASSWORD_TRIGGER_CLASS}"` +
    ` data-id="${row.id}" lay-tips="还没有设置平台支付密码，点击快速设置">点击设置</span>`
  )
}

/**
 * 表格列（顺序 / 标题 / 宽度 / 对齐 / 开关模板引用**逐条照** `tripartiteplatformsetting.js:66-111`）。
 *
 * ⚠️ 「支付密码」列必须是**函数 templet**（见文件头差异 #2）；「平台名称」列也是函数
 * （多那枚「暂未开通」徽标，见差异 #3）。
 * ⚠️ 5 个开关列仍然引用证据件里的 `#switchTpl1-5`（模板原文一个字没改）。
 */
export function buildIntegrationCols(): Record<string, unknown>[] {
  return [
    {
      field: 'platformName',
      title: '平台名称',
      width: 105,
      align: 'center',
      templet: (row: IntegrationTableRow) => platformNameCell(row),
    },
    { field: 'platformAcc', title: '平台账号/ID', width: 170, align: 'left' },
    { field: 'appId', title: 'AppId', width: 150, align: 'left' },
    {
      field: 'isFillPlatmPayPwd',
      title: '支付密码',
      width: 100,
      align: 'center',
      templet: (row: IntegrationTableRow) => payPasswordCell(row),
    },
    {
      field: 'status',
      title: '是否启用',
      width: 100,
      align: 'center',
      templet: '#switchTpl1',
    },
    {
      field: 'isDefault',
      title: '默认账号',
      width: 100,
      align: 'center',
      templet: '#switchTpl5',
    },
    {
      field: 'isAutoAccep',
      title: '同步验收',
      width: 100,
      align: 'center',
      templet: '#switchTpl2',
    },
    {
      field: 'isNotifyPushMan',
      title: '接单短信通知',
      width: 105,
      align: 'center',
      templet: '#switchTpl3',
    },
    {
      field: 'isAutoSelect',
      title: '发单包含',
      width: 100,
      align: 'center',
      templet: '#switchTpl4',
    },
    { field: 'concactQQ', title: '发单联系QQ', width: 120, align: 'left' },
    { field: 'concactPhone', title: '发单联系电话', width: 120, align: 'left' },
    { field: 'remark', title: '备注说明', width: 100, align: 'left' },
    { title: '操作', width: 170, align: 'center', fixed: '', toolbar: '#baroperating' },
  ]
}

/** 一个 `table.render` 的选项子集（逐条照抄对标页面 JS）。 */
type TableOptions = {
  elem: string
  toolbar?: string
  height?: number | string
  page?: false | { limit: number; limits: number[] }
  defaultToolbar?: string[]
  cellMinWidth?: number
  totalRow?: boolean
  cols: [Record<string, unknown>[]]
}

/** 对标的表格配置（对齐字段由 `buildIntegrationCols` 给）。 */
const TABLES: TableOptions[] = [
  {
    elem: TABLE_ELEM,
    toolbar: '#toolbarDemo',
    height: 'full',
    // 分页出处：对标 L112 `page: false`。
    page: false,
    defaultToolbar: ['filter'],
    cols: [buildIntegrationCols()],
  },
]

/** 导出给 spec 做「与对标 `cols` 不漂移」的对拍（`__tests__/system-tripartiteplatformsetting.spec.ts`）。 */
export const TABLES_FOR_TEST: TableOptions[] = TABLES

/* ────────────────────────────── 9 组全局设置 ────────────────────────────── */

type BooleanSettingsField =
  | 'quick_push_enabled'
  | 'forbid_push_when_double_gold_zero'
  | 'select_jidanpu_center_by_default'
  | 'auto_zero_refund_on_conflict'
type ZeroOneSettingsField =
  | 'legacy_simple_mode'
  | 'check_game_region'
  | 'duplicate_push_check'
  | 'block_push_completed_orders'
  | 'auto_send_group_qr_after_accept'

type SettingsRadioGroup =
  | { name: string; field: BooleanSettingsField; kind: 'boolean'; effective: boolean }
  | { name: string; field: ZeroOneSettingsField; kind: 'zeroOne'; effective: boolean }

/**
 * 证据件 html:149-247 的 9 组单选 → 后端字段的映射（**唯一出处**）。
 *
 * | 组 | 证据件 `name` | 证据件 `value` | 后端字段 | 编码 | 已生效？ |
 * |---|---|---|---|---|---|
 * | 1 | `SK132` | `N` / `Y`（默认 `Y`） | `quick_push_enabled` | 布尔（`Y` = true） | ❌ 仅保存 |
 * | 1.1 | `SK156` | `0` / `1`（默认 `0`） | `legacy_simple_mode` | 0/1 原值 | ❌ 仅保存 |
 * | 1.2 | `SK157` | `0` / `1`（默认 `0`） | `check_game_region` | 0/1 原值 | ❌ 仅保存 |
 * | 2 | `SK107` | `Y` / `N`（默认 `N`） | `forbid_push_when_double_gold_zero` | 布尔 | ❌ 仅保存 |
 * | 2.1 | `SK159` | `0` / `1`（默认 `0` = 检测） | `duplicate_push_check` | 0/1 原值 | ❌ 仅保存 |
 * | 3 | `SK106` | `Y` / `N`（默认 `N`） | `select_jidanpu_center_by_default` | 布尔 | ❌ 仅保存 |
 * | 4 | `SK145` | `Y` / `N`（默认 `Y`） | `auto_zero_refund_on_conflict` | 布尔 | ✅ **已生效**（撞单 0 撤） |
 * | 5 | `SK155` | `0` / `1`（默认 `0` = 不限制） | `block_push_completed_orders` | 0/1 原值 | ❌ 仅保存 |
 * | 6 | `SK158` | `0` / `1`（默认 `0` = 不发送） | `auto_send_group_qr_after_accept` | 0/1 原值 | ✅ **已生效** |
 *
 * ⚠️ `effective` 是 **F6 的唯一数据源**（计划 §3 那张表）：发单 / 同步链路真的读了它才算 ✅。
 * ⭐ 2026-10-28（H3-1）：第 4 条 `SK145` 也变成 ✅ —— 读取点是
 * `dailiantong_push.zero_refund_conflicting_pushes`（每轮 `dailiantong_sync` 的一段：
 * 撞单保留最早接单、其余 0 撤；开关关 ⇒ 零上游调用）。
 * 另外三项已生效的配置（要求最低完单数 / 拒绝新手和介入率 / 代练要求模板）不在这一块里，
 * 它们在发单页（`order-pushjdp.ts`）；⛔ 不要为了让这张表好看把它们塞进这 9 组。
 */
export const SETTINGS_GROUPS: readonly SettingsRadioGroup[] = [
  { name: 'SK132', field: 'quick_push_enabled', kind: 'boolean', effective: false },
  { name: 'SK156', field: 'legacy_simple_mode', kind: 'zeroOne', effective: false },
  { name: 'SK157', field: 'check_game_region', kind: 'zeroOne', effective: false },
  { name: 'SK107', field: 'forbid_push_when_double_gold_zero', kind: 'boolean', effective: false },
  { name: 'SK159', field: 'duplicate_push_check', kind: 'zeroOne', effective: false },
  { name: 'SK106', field: 'select_jidanpu_center_by_default', kind: 'boolean', effective: false },
  { name: 'SK145', field: 'auto_zero_refund_on_conflict', kind: 'boolean', effective: true },
  { name: 'SK155', field: 'block_push_completed_orders', kind: 'zeroOne', effective: false },
  { name: 'SK158', field: 'auto_send_group_qr_after_accept', kind: 'zeroOne', effective: true },
]

/** 读模型 → 页面该勾哪个 radio 的 `value`（纯函数；布尔组的 `Y/N` 在这里唯一决定）。 */
export function settingsRadioValue(
  group: SettingsRadioGroup,
  settings: PlatformPushSettings,
): string {
  if (group.kind === 'boolean') return settings[group.field] ? 'Y' : 'N'
  return String(settings[group.field])
}

/**
 * 把后端值回填到证据件的 9 组单选框上（**先把整组清空再勾**，否则证据件里
 * 服务端渲染的 `checked` 默认值会与真值并存、radio 变成"两个都选中"的假象）。
 */
export function fillSettingsRadios(root: HTMLElement, settings: PlatformPushSettings): void {
  for (const group of SETTINGS_GROUPS) {
    const wanted = settingsRadioValue(group, settings)
    const inputs = root.querySelectorAll<HTMLInputElement>(`input[name="${group.name}"]`)
    // 证据件缺这一组 = 前后端对不上：静默跳过会让页面显示默认值、看起来"保存过了"。
    if (inputs.length === 0) throw new Error(`页面上找不到第 ${group.name} 组设置`)
    inputs.forEach((input) => {
      input.checked = input.value === wanted
    })
  }
}

/**
 * 从 9 组单选读回请求载荷（数值组给 **number**，不给字符串——后端 `ChoiceField` 收的是
 * `0/1` 的整数值；发 `"0"` 在 DRF 里虽然可能被强转，但那不是契约）。
 */
export function settingsRadioPayload(root: HTMLElement): Partial<PlatformPushSettings> {
  const payload: Record<string, boolean | number> = {}
  for (const group of SETTINGS_GROUPS) {
    const selected = root.querySelector<HTMLInputElement>(`input[name="${group.name}"]:checked`)
    if (!selected) throw new Error(`第 ${group.name} 组设置没有选中值，请重新选择`)
    if (group.kind === 'boolean') payload[group.field] = selected.value === 'Y'
    else payload[group.field] = Number(selected.value)
  }
  return payload as Partial<PlatformPushSettings>
}

/**
 * 把 9 组单选**全部取消勾选**（读设置失败时用）。
 *
 * ⚠️ 证据件的 `checked` 是**对标服务端的默认值**；读真值失败却继续显示它们，就是拿默认值
 * 冒充真值（标准 §5：不假装成功）。宁可九个组都空着 + 一句错误提示，让用户知道没读到。
 */
export function clearSettingsRadios(root: HTMLElement): void {
  for (const group of SETTINGS_GROUPS) {
    root.querySelectorAll<HTMLInputElement>(`input[name="${group.name}"]`).forEach((input) => {
      input.checked = false
    })
  }
}

/**
 * 在「对所有平台生效」那一块上方插入 **F6 的生效范围声明 + 图例**。
 *
 * ⚠️ 上一版的文案「本版仅保存，不影响发单」**已不成立**（第 6 条自 2026-10-27 起真生效），
 * 按业务方要求改成「只有第 6 条已生效」＋逐组标注（`annotateSettingsEffects`）。
 * 不写清楚就是「写操作假装成功」的反面——把真生效的说成不生效，客服同样会误判。
 */
export function insertSettingsNotice(root: HTMLElement): void {
  if (root.querySelector(`#${SETTINGS_NOTICE_ID}`)) return
  const block = root.querySelector<HTMLElement>('.layui-bg-gray')
  if (!block) return
  const notice = document.createElement('blockquote')
  notice.id = SETTINGS_NOTICE_ID
  notice.className = 'layui-elem-quote'
  notice.setAttribute('style', 'border-left:5px solid #FFB800;background:#FFFBF0')
  notice.innerHTML =
    `<b style="color:#FF5722">${SETTINGS_NOTICE_TEXT}</b><br>` +
    `<span style="color:#666">${SETTINGS_EFFECT_LEGEND}</span>`
  const quote = block.querySelector('blockquote.layui-elem-quote')
  if (quote) quote.insertAdjacentElement('afterend', notice)
  else block.prepend(notice)
}

/**
 * F6：给 9 组设置**逐组**打上「已生效 / 仅保存」标注（计划 §3 那张表）。
 *
 * 标注插在每组第一行说明文字的前面（`span.layui-badge`，绿 = 已生效、橙 = 仅保存）。
 * ⚠️ 缺组时**响亮失败**（与 `fillSettingsRadios` 同口径）：静默跳过 = 有一组没标注，
 * 用户会以为它也是「已生效」。
 */
export function annotateSettingsEffects(root: HTMLElement): void {
  for (const group of SETTINGS_GROUPS) {
    const input = root.querySelector(`input[name="${group.name}"]`)
    const item = input?.closest('.layui-form-item')
    const description = item?.querySelector<HTMLElement>('.layui-input-block')
    if (!description) throw new Error(`页面上找不到第 ${group.name} 组设置的说明块`)
    if (description.querySelector(`[${SETTINGS_EFFECT_ATTR}="${group.name}"]`)) continue
    const badge = document.createElement('span')
    badge.className = group.effective
      ? `layui-badge layui-bg-green ${SETTINGS_EFFECT_ON_CLASS}`
      : `layui-badge layui-bg-orange ${SETTINGS_EFFECT_OFF_CLASS}`
    badge.setAttribute(SETTINGS_EFFECT_ATTR, group.name)
    badge.textContent = group.effective ? SETTINGS_EFFECT_ON_TEXT : SETTINGS_EFFECT_OFF_TEXT
    badge.style.marginRight = '6px'
    description.insertBefore(badge, description.firstChild)
  }
}

/* ────────────────────────────── 交互辅助 ────────────────────────────── */

function errText(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败'
}

function panelOf(root: HTMLElement): HTMLElement | null {
  return findPanel(root, PANEL_SELECTOR)
}

function fieldValue(panel: HTMLElement, name: string): string {
  return panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)?.value ?? ''
}

function setFieldValue(panel: HTMLElement, name: string, value: string): void {
  const input = panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)
  if (input) input.value = value
}

function checkedPlatform(panel: HTMLElement): number {
  const selected = panel.querySelector<HTMLInputElement>('input[name="platforms"]:checked')
  return Number(selected?.value ?? '0')
}

/** 一次 `PATCH` 一个开关（5 个开关共用；`payload` 由调用方给，避免拼错字段）。 */
function switchPayload(
  field: 'is_enabled' | 'is_default' | 'sync_accept' | 'sms_notify' | 'include_push',
  checked: boolean,
): PlatformIntegrationPayload {
  return { [field]: checked } as unknown as PlatformIntegrationPayload
}

async function mountPage(root: HTMLElement, ctx: Parameters<IslandPage['mount']>[1]): Promise<() => void> {
  return mountConfigLike(
    root,
    {
      key: PAGE_KEY,
      title: TITLE,
      tableSpec: {
        elem: TABLE_ELEM,
        toolbar: '#toolbarDemo',
        height: 'full',
        defaultToolbar: ['filter'],
        page: false,
        cols: TABLES[0]!.cols,
      },
      onReady: ({ root: pageRoot, layui, form, table, layer }) => {
        const msg = (text: string): void =>
          islandMsg(layer as unknown as Record<string, unknown>, text)
        const renderForm = (type?: string): void =>
          (form.render as (t?: string) => void)(type)
        const reloadData = (rows: IntegrationTableRow[]): void =>
          (table.reloadData as (id: string, options: unknown) => void)(TABLE_ID, { data: rows })

        /** 正在编辑的那一行；`null` = 新增。判据用「有没有这一行」，不用 `#ptid` 的字符串。 */
        let editing: PlatformIntegrationRow | null = null
        /** 当前打开的弹层索引（关闭 / 卸载时用）。 */
        let layerIndex: number | null = null
        /**
         * 保存是否在途（**承重闸**，不是防御性写法）：连点两次会发两发 POST，而这张表
         * 不限「同平台唯一」⇒ 多出一条重复账号。发单页同款双保险 = 旗标 + 按钮禁用。
         */
        let submitting = false
        /** 9 组全局设置的保存闸（同一颗 `lay-submit` 连点也只 PUT 一次）。 */
        let settingsSubmitting = false
        /**
         * 9 组全局设置**是否已经成功读到后端真值**。
         *
         * ⚠️ 这是承重闸，不是防御性写法：证据件里的单选自带 `checked`（= 对标服务端的默认值），
         * 在 `GET settings` 返回之前它们就是「可提交态」——此时点保存会把 9 个**默认值** PUT 回去，
         * 覆盖租户已经存好的真实配置（数据丢失方向）。所以读到真值之前**不允许提交**，
         * 点击给明确提示，⛔ 不静默成功。读失败时这个标志保持 `false`。
         */
        let settingsLoaded = false

        insertSettingsNotice(pageRoot)
        // F6：逐组标注「已生效 / 仅保存」（计划 §3 那张表）。
        annotateSettingsEffects(pageRoot)

        async function reloadList(): Promise<void> {
          const rows = await fetchPlatformIntegrations()
          reloadData(rows.map(integrationToTableRow))
        }

        async function loadSettings(): Promise<void> {
          const settings = await fetchPlatformPushSettings()
          fillSettingsRadios(pageRoot, settings)
          renderForm('radio')
          // 只有**回填成功**之后才放开保存闸（见 `settingsLoaded` 的注释）。
          settingsLoaded = true
        }

        /**
         * 打开新增 / 修改弹层。
         *
         * ⚠️ **顺序是硬要求：「先预填、再 open」**——`layer.open({type:1, content: …})` 会把
         * `.modifyParamDiv` 搬进 `.layui-layer-content`（挂在 `document.body` 下），
         * 先 open 再填就会在 `root` 里找不到节点（先例 `system-pddshipsetting.ts`）。
         * 平台 radio 的勾选态也要在 open 之前设好并 `form.render('radio')`——搬走之后
         * layui 的 `form.render` 按 `.layui-form` 作用域找节点，找不到就画不出来。
         */
        function openForm(row: PlatformIntegrationRow | null): void {
          editing = row
          if (layerIndex !== null) {
            ;(layer.close as (index: number) => void)(layerIndex)
            layerIndex = null
          }
          const panel = panelOf(pageRoot)
          if (!panel) {
            // 节点缺失：只提示，不假装打开了（绝不静默）。
            msg('弹窗内容缺失，请联系管理员')
            return
          }
          const platform = row?.platform ?? 0
          for (const radio of panel.querySelectorAll<HTMLInputElement>('input[name="platforms"]')) {
            radio.checked = radio.value === String(platform)
          }
          applyPlatformFields(panel, platform)
          ensureLoginPasswordNotice(panel)
          setFieldValue(panel, 'plaftacc', row?.account ?? '')
          // ⚠️ 两个密码框**永远留空**：AppSecret 后端不回；支付密码只存 MD5、也不回。
          setFieldValue(panel, 'plaftloginpwd', '')
          setFieldValue(panel, 'plaftpaypwd', '')
          setFieldValue(panel, 'appId', row?.app_id ?? '')
          setFieldValue(panel, 'appSecret', '')
          setFieldValue(panel, 'remark', row?.remark ?? '')
          setFieldValue(panel, 'concactQQ', row?.contact_qq ?? '')
          setFieldValue(panel, 'concactPhone', row?.contact_phone ?? '')
          setFieldValue(panel, 'ptid', row ? String(row.id) : '0')
          const payInput = panel.querySelector<HTMLInputElement>('input[name="plaftpaypwd"]')
          if (payInput) {
            payInput.placeholder = row
              ? '若没有改过，请不要动此处'
              : '请输入交易平台的支付密码'
          }
          renderForm('radio')
          layerIndex = openPanelLayer(
            pageRoot,
            layui,
            layer,
            {
              title: row ? '编辑对接' : '新增一个代练平台对接',
              shade: [0.3, '#333'],
              shadeClose: false,
              // 桌面后台（本项目只做 PC）⇒ 取对标 `isPc` 分支的尺寸。
              area: ['676px', '620px'],
              skin: 'layui-layer-lan',
              anim: 8,
            },
            PANEL_SELECTOR,
          )
        }

        /** 「保存设置」：照对标 `$('.saveInterfaceBtn').on('click')` 的校验 + 载荷。 */
        function submitForm(): void {
          if (submitting) return
          const panel = panelOf(pageRoot)
          if (!panel) return
          const platform = checkedPlatform(panel)
          if (!platform) {
            msg('请选择一个平台')
            return
          }
          const account = fieldValue(panel, 'plaftacc').trim()
          const appId = fieldValue(panel, 'appId').trim()
          // 对标 L340-349：先选平台，再对「除 92 / 50 / 93 / 8 以外」要求 AppId。
          if (!APPID_OPTIONAL_PLATFORMS.has(platform) && appId === '') {
            msg('AppId不能为空')
            return
          }
          const appSecret = fieldValue(panel, 'appSecret')
          // ⚠️ **支付密码必须 trim 后再判空**（2026-10-27 审计 P2-8）：纯空格会被后端 MD5 成
          // 一个「真密码」，之后发单一直报密码错，而用户以为自己没设过。空白 = 没填。
          const payPassword = fieldValue(panel, 'plaftpaypwd').trim()
          const loginPassword = fieldValue(panel, 'plaftloginpwd')
          const payload: PlatformIntegrationPayload = {
            platform,
            account,
            app_id: appId,
            remark: fieldValue(panel, 'remark'),
            contact_qq: fieldValue(panel, 'concactQQ'),
            contact_phone: fieldValue(panel, 'concactPhone'),
          }
          // 空串 = 不改（后端约定）⇒ 连键都不带；支付密码空串会被后端**拒绝**（明确写操作）。
          if (appSecret !== '') payload.app_secret = appSecret
          if (payPassword !== '') payload.pay_password = payPassword

          const target = editing
          // 双保险：旗标挡住第二次点击，按钮禁用让人**看得见**它已经点过了。
          submitting = true
          const saveButton = panel.querySelector<HTMLElement>('.saveInterfaceBtn')
          if (saveButton) {
            saveButton.classList.add('layui-btn-disabled')
            saveButton.setAttribute('aria-disabled', 'true')
          }
          const finish = (): void => {
            submitting = false
            if (saveButton) {
              saveButton.classList.remove('layui-btn-disabled')
              saveButton.removeAttribute('aria-disabled')
            }
          }
          const request = target
            ? updatePlatformIntegration(target.id, payload)
            : createPlatformIntegration(payload)
          // ⚠️ 两条失败**分开说**：保存已经成功、只是列表刷新失败时，如果也报「保存失败」，
          // 用户会以为没保存而**再提交一次**（弹层这时已经关了）。用两参数 `.then` 而不是
          // `.then(...).catch(...)`，后者会把刷新失败也归到保存失败上。
          void request.then(
            (saved) => {
              finish()
              if (layerIndex !== null) {
                ;(layer.close as (index: number) => void)(layerIndex)
                layerIndex = null
              }
              editing = null
              let text = '已保存'
              if (!saved.is_available) text += '；该平台暂未开通，本版只保存参数'
              if (loginPassword !== '' && platformFieldConfig(platform).showLoginPassword) {
                text += '；「平台登录密码」尚未开放，未随本次保存提交'
              }
              void reloadList().then(
                () => msg(text),
                (error: unknown) => msg(`${text}；但列表刷新失败：${errText(error)}`),
              )
            },
            (error: unknown) => {
              finish()
              msg(`保存失败：${errText(error)}`)
            },
          )
        }

        /** 删除（确认语与按钮逐字照 `tripartiteplatformsetting.js:191-193`）。 */
        function removeRow(row: PlatformIntegrationRow): void {
          ;(layer.confirm as (
            text: string,
            options: unknown,
            onConfirm: (index: number) => void,
          ) => void)(
            '是否确认删除这个对接账号？提示：删除此对接账号不会影响之前用这个账号发过单的交易操作',
            { btn: ['确认删除', '点错了'] },
            (index: number) => {
              ;(layer.close as (i: number) => void)(index)
              void deletePlatformIntegration(row.id).then(
                () => {
                  void reloadList().then(
                    () => msg('已删除'),
                    (error: unknown) => msg(errText(error)),
                  )
                },
                (error: unknown) => msg(errText(error)),
              )
            },
          )
        }

        /** 支付密码：点「对勾 / 点击设置」→ `layer.prompt` 收新值 → `PATCH`（逐字照对标 L409-435）。 */
        const onPageClick = (event: Event): void => {
          const target = event.target as HTMLElement | null
          const trigger = target?.closest?.(PAY_PASSWORD_TRIGGER_SELECTOR)
          if (!(trigger instanceof HTMLElement)) return
          event.preventDefault()
          const rowId = Number(trigger.dataset.id)
          if (!Number.isInteger(rowId) || rowId <= 0) {
            msg('这一行的支付密码入口无效，请刷新后重试')
            return
          }
          ;(layer.prompt as (
            options: unknown,
            onOk: (value: string, index: number) => void,
          ) => void)(
            {
              title: '请填充平台的支付密码',
              shade: [0.3, '#333'],
              shadeClose: true,
              formType: 0,
              anim: 2,
              value: '',
            },
            (value: string, index: number) => {
              // ⚠️ 先 trim（2026-10-27 审计 P2-8）：纯空格被 MD5 后就是一个「真密码」，
              // 之后发单一直报密码错。空白按空处理。
              const password = String(value ?? '').trim()
              // 空串在后端是**明确的写操作**并被拒（那是为了不把"清空"变成静默 no-op）⇒ 本地先拦。
              if (password === '') {
                msg('支付密码不能为空')
                return
              }
              void updatePlatformIntegration(rowId, { pay_password: password }).then(
                () => {
                  ;(layer.close as (i: number) => void)(index)
                  void reloadList().then(
                    () => msg('支付密码已设置'),
                    (error: unknown) => msg(errText(error)),
                  )
                },
                (error: unknown) => msg(errText(error)),
              )
            },
          )
        }
        pageRoot.addEventListener('click', onPageClick)

        /* ── 表格：工具栏 / 行内按钮 ── */
        ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
          `toolbar(${TABLE_ID})`,
          (obj: unknown) => {
            const event = (obj as { event?: string }).event
            if (event === 'refresh') {
              void reloadList().catch((error: unknown) => msg(errText(error)))
            } else if (event === 'addBtn') {
              openForm(null)
            }
          },
        )
        ;(table.on as (event: string, cb: (obj: unknown) => void) => void)(
          `tool(${TABLE_ID})`,
          (obj: unknown) => {
            const typed = obj as { event?: string; data?: PlatformIntegrationRow }
            const row = typed.data
            if (!row) return
            if (typed.event === 'submitEdit') openForm(row)
            else if (typed.event === 'submitDel') removeRow(row)
          },
        )

        /* ── 5 个行内开关：勾选态 = 后端布尔（模板的反向编码只在 `integrationToTableRow` 里）── */
        const bindSwitch = (
          filter: string,
          field: 'is_enabled' | 'is_default' | 'sync_accept' | 'sms_notify' | 'include_push',
        ): void => {
          ;(form.on as (event: string, cb: (obj: unknown) => void) => void)(
            `switch(${filter})`,
            (obj: unknown) => {
              const elem = (obj as { elem?: HTMLInputElement }).elem
              if (!elem) return
              const rowId = Number(elem.value)
              if (!Number.isInteger(rowId) || rowId <= 0) return
              const checked = elem.checked
              void updatePlatformIntegration(rowId, switchPayload(field, checked)).then(
                // 默认开关会让同平台别的行被降级 ⇒ 必须整表重取，不能只改本地这一格。
                () => reloadList().catch((error: unknown) => msg(errText(error))),
                (error: unknown) => {
                  // 失败时把勾选态拨回去（不留一个"看起来保存了"的假象）；随后重取以服务端为准。
                  elem.checked = !checked
                  msg(errText(error))
                  void reloadList().catch(() => undefined)
                },
              )
            },
          )
        }
        bindSwitch('staChecked', 'is_enabled')
        bindSwitch('defaultChecked', 'is_default')
        bindSwitch('isAutoAccepChecked', 'sync_accept')
        bindSwitch('isNotifyPushManChecked', 'sms_notify')
        bindSwitch('isAutoSelectChecked', 'include_push')

        /* ── 弹层里的平台切换：照 `form.on('radio(platforms)')` 改 label / 显隐 ── */
        ;(form.on as (event: string, cb: (obj: unknown) => void) => void)(
          'radio(platforms)',
          (obj: unknown) => {
            const value = Number((obj as { value?: string }).value ?? '0')
            const panel = panelOf(pageRoot)
            if (!panel) return
            applyPlatformFields(panel, value)
            ensureLoginPasswordNotice(panel)
          },
        )

        /* ── 弹层里的「保存设置」──
         * 证据件那颗是 `<a class="… saveInterfaceBtn">`（不是 `lay-submit`）；
         * 节点会被搬进 `.layui-layer-content` ⇒ 委托挂在**节点自己**身上（搬走也不丢）。
         */
        const onPanelClick = (event: Event): void => {
          const target = event.target as HTMLElement | null
          if (!target?.closest?.('.saveInterfaceBtn')) return
          event.preventDefault()
          submitForm()
        }
        panelOf(pageRoot)?.addEventListener('click', onPanelClick)

        /* ── 9 组全局设置的保存 ── */
        ;(form.on as (event: string, cb: () => void) => void)(
          `submit(${SETTINGS_SAVE_FILTER})`,
          () => {
            // 读真值之前证据件里那 9 个 `checked` 是**对标默认值**，直接提交会覆盖租户配置。
            if (!settingsLoaded) {
              msg('设置还没读取完成，请稍后再试（或刷新页面）')
              return
            }
            if (settingsSubmitting) return
            let payload: Partial<PlatformPushSettings>
            try {
              payload = settingsRadioPayload(pageRoot)
            } catch (error: unknown) {
              msg(errText(error))
              return
            }
            settingsSubmitting = true
            const saveButton = pageRoot.querySelector<HTMLElement>(
              `button[lay-filter="${SETTINGS_SAVE_FILTER}"]`,
            )
            if (saveButton) {
              saveButton.classList.add('layui-btn-disabled')
              saveButton.setAttribute('aria-disabled', 'true')
            }
            const finish = (): void => {
              settingsSubmitting = false
              if (saveButton) {
                saveButton.classList.remove('layui-btn-disabled')
                saveButton.removeAttribute('aria-disabled')
              }
            }
            void savePlatformPushSettings(payload).then(
              (saved) => {
                finish()
                // 回填**后端回的那份**（不是我们刚发出去的值），再提示。
                fillSettingsRadios(pageRoot, saved)
                renderForm('radio')
                msg('已保存')
              },
              (error: unknown) => {
                finish()
                msg(errText(error))
              },
            )
          },
        )

        // 两路取数互不拖累：列表挂了也要能看到设置（反之亦然），各自把错误说出来。
        void reloadList().catch((error: unknown) => msg(errText(error)))
        void loadSettings().catch((error: unknown) => {
          // 读不到真值 ⇒ 把证据件的默认勾选清掉（否则看起来像"后端值就是这样"）。
          clearSettingsRadios(pageRoot)
          renderForm('radio')
          msg(errText(error))
        })

        return () => {
          pageRoot.removeEventListener('click', onPageClick)
          panelOf(pageRoot)?.removeEventListener('click', onPanelClick)
          if (layerIndex !== null) {
            ;(layer.close as (index: number) => void)(layerIndex)
            layerIndex = null
          }
        }
      },
    },
    ctx,
  )
}

export default {
  mount: mountPage,
} satisfies IslandPage
