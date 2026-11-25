/**
 * 【自助注册】island 页（island key = `self-registration`，路由 `/self-registration`）。
 *
 * spec：`docs/specs/2026-10-16-self-registration-links-design.md` §4（本页）/ §3（端点契约）。
 * 契约的另一半是**实现即契约**：`src/apps/accounts/selfreg_views.py`（T-后端线并行实现）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）的人员管理组没有这一页 ⇒ 本页**没有证据件**、也不走 `mountConfigLike`
 * / `artifactPage`（那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。
 * 标记由本文件自己写，风格照 `pages/feedback.ts` 与 `pages/user-adduser.ts` 的 layui 惯例。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## 一页三卡（spec §4）
 *
 * 1. **注册配置**：游戏 `lay-skin="tag"` checkbox（数据源 `GET /api/business/games/`，
 *    走 `@/api/config` 的 `fetchAllGames`——与「新开系统账号」同一份全量出口：配置页要能
 *    勾上**当前没勾**的游戏，按权限收窄就是「看不到 ⇒ 勾不上」的死锁）+
 *    「新注册账号默认开启『禁自助接单』」开关（复用现成开关 `StaffProfile.grab_forbid_self_service`
 *    的语义，spec §1）+ 「保存配置」。
 *    ⚠️ 本页**不再造**游戏 id 与业务.Game 的对拍：`game_ids ≥ 1 且都在本租户` 判在**服务端**
 *    （spec §3.1）；前端只做「一个都没勾就不发请求」的**体验层**预检（文案带字段名）。
 * 2. **生成注册链接**：说明文字（一次性 · 7 天有效 · 按保存时的配置固化）+ 按钮「生成并复制链接」
 *    → `POST invites` → 拼 `location.origin + '/m/register?tk=' + token` → `copyToClipboard`
 *    → 成功 toast + 页面**显示完整链接**（复制失败时用户还能手动复制——链接只显式出现一次，
 *    静默丢掉就得重新生成）。
 *    ⛔ **token 明文只在响应里出现一次**（spec §3.2）：拼进链接后 ⛔ 不写日志、不发往别的端点。
 * 3. **链接与注册记录**：刷新按钮 + 表格，8 列 = 生成时间 / 生成人（开户人）/ 状态（未使用·
 *    已使用·已过期）/ 有效期至 / 注册进来的人 / 注册时间 / 勾选游戏（未使用显示「—」）/ 操作
 *    （**只有未使用行**画「作废」按钮——已消费 / 已过期的行没有可作废的东西，D-0253）。
 *    状态列用**色标**区分（色值取自 vendored `layui.css`，与 feedback 页同一套做法，
 *    测试对拍该文件——⛔ 不凭记忆写色值）。
 *    「作废」= `POST invites/<id>/revoke/`（立即过期），**两跳 confirm** 照
 *    `system-user-list.ts` 删除用户的既有形状：两连问都确认才发请求。
 *
 * ## ⚠️ 控件是照 `user-adduser.ts` 的形状抄的，⛔ 不是 import 它
 *
 * `gamesMarkup` / `readCheckedGames` / `copyToClipboard` 三件的形状**照抄**那一页
 * （业务方点名），但**不 import**：跨页面 import 会把整页连同它的证据件依赖一起拖进来
 * （`escape.ts` 文件头记过同一条理由）。代价如实登记：**三处同形的小函数**，改一处要想到另一处。
 * ⛔ 不引第三方剪贴板库（`navigator.clipboard` + `execCommand` 兜底已覆盖同一片浏览器）。
 *
 * ## ⚠️ 权限只有一道门，页内自检与它同源
 *
 * 整页被路由门挡住（门 = 节点 `system_user.add_edit`，spec §4「页内不再二次收窄」）；
 * 页内的 `canNode` 自检**只是 iframe 深链那一侧的同一道门**（iframe 读不到 Vue 路由的
 * `meta`，先例 = `pages/feedback.ts`），⛔ 不是第二套判据。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(form.render as …)(…)`）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）→ 预填 + `form.render()`
 *    （layui 渲染读的是**塞进去之后**的原生 `checked` 属性，`user-adduser.ts` 同一条纪律）。
 * ⚠️ 三条都有**运行期**判据（`__tests__/self-registration.spec.ts`），不是靠这段注释。
 */
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { fetchMe } from '@/api/auth'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import {
  createSelfRegInvite,
  fetchSelfRegConfig,
  listSelfRegInvites,
  revokeSelfRegInvite,
  saveSelfRegConfig,
  type SelfRegConfig,
  type SelfRegInviteRow,
  type SelfRegInviteStatus,
} from '@/api/selfRegistration'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'self-registration'
export const TITLE = '自助注册'

/**
 * 本页的 RBAC 节点 code（**不是新造的**：与「新开系统账号 / 编辑系统账号」同一个点，
 * spec §6 直接裁定 `'self-registration': ['system_user.add_edit']`）。
 * ⚠️ 取值的唯一权威是 `config/routePermissions.ts` 的 `ROUTE_NODE_CODES['self-registration']`；
 * 这里是 **iframe 内的深链自检**用的同一份字面量（iframe 读不到 Vue 路由的 `meta`）。
 * 两处漂移由 `__tests__/self-registration.spec.ts` 的一条断言钉住（⛔ 不是靠这句话）。
 */
export const SELF_REG_NODE = 'system_user.add_edit'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'selfRegPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`
/**
 * **注册配置**容器的类名 / 选择器 / filter。
 *
 * ⚠️ 它同时是 `layui-form`（`form.render` 的根选择器就是 `.layui-form`）＋ 带 `lay-filter`
 * ⇒ 渲染 tag checkbox / switch 时可以只重渲这一块，⛔ 不用无参的全局渲染。
 */
export const FORM_CLASS = 'selfRegConfigForm'
export const FORM_SELECTOR = `.${FORM_CLASS}`
export const FORM_FILTER = 'selfRegConfigForm'
/** 生成链接卡与记录卡容器（记录表**不是** layui 表单控件，不放进 form 根里）。 */
export const INVITE_CLASS = 'selfRegInvite'
export const INVITE_SELECTOR = `.${INVITE_CLASS}`
export const LIST_CLASS = 'selfRegList'
export const LIST_SELECTOR = `.${LIST_CLASS}`

/** 配置卡里的控件 `id`。 */
export const GAMES_BOX_ID = 'selfRegGamesBox'
export const FORBID_SWITCH_ID = 'selfRegForbidSwitch'
/** 编号两栏（spec §2.1：「编号前缀（字母）」「起始编号」）。 */
export const NUMBER_PREFIX_INPUT_ID = 'selfRegNumberPrefix'
export const NUMBER_START_INPUT_ID = 'selfRegNumberStart'
export const SAVE_BUTTON_ID = 'selfRegSaveBtn'
/** 生成链接卡里的控件 `id`。 */
export const GENERATE_BUTTON_ID = 'selfRegGenerateBtn'
export const LINK_BOX_ID = 'selfRegLinkBox'
/** 记录卡里的控件 `id`（表头一行 + 行容器 + 空态）。 */
export const REFRESH_BUTTON_ID = 'selfRegRefreshBtn'
export const LIST_HEAD_ID = 'selfRegListHead'
export const LIST_BODY_ID = 'selfRegListBody'

/** 记录表**每行**的类名（选择器由常量拼，⛔ 不写字面串——陷阱 ① 的同一条纪律）。 */
export const ROW_CLASS = 'selfRegRow'
/**
 * 行内「作废」按钮的**类名**与**选择器**（陷阱 ①：两个常量，别混）。
 * ⚠️ 只渲染在 `status === 'unused'` 的行上（已消费 / 已过期的行不画，D-0253）；
 * 点击走 `listBody` 上的**事件委托**（行是 `reload()` 重铺的 innerHTML，
 * 逐行挂监听会随重铺丢失）。
 */
export const REVOKE_BUTTON_CLASS = 'selfRegRevokeBtn'
export const REVOKE_BUTTON_SELECTOR = `.${REVOKE_BUTTON_CLASS}`
/** 勾选游戏的 checkbox `name`（照 `user-adduser.ts` 的 `readCheckedGames` 同一形状）。 */
export const GAMES_INPUT_NAME = 'games'
/** 开关的 `name`（读态与预填都按它定位，⛔ 不靠 id 以外第二套写法）。 */
export const FORBID_SWITCH_NAME = 'forbidOrdersDefault'

/**
 * 编号前缀的取值形状（spec §2.1：只允许字母）。⚠️ 判在**服务端**（同一正则、
 * 同一句文案），这里只是**体验层**预检——两处各是各的出口，文案取自
 * `@/api/selfRegistration` 契约里的服务端判据（⛔ 不是本页发明第二句）。
 */
const NUMBER_PREFIX_RE = /^[A-Za-z]{1,5}$/

/** 编号两栏的读态（`number_start` 没填 = `null`，与契约的 `number | null` 同形）。 */
export type NumberConfigInput = { number_prefix: string; number_start: number | null }

/**
 * 读编号两栏（**DOM 值 → 契约形状**的唯一出口）。
 * ⚠️ `number_start` 非法输入（非整数）**不在**这里静默归一成 `null`——那会把
 * 「没填」与「填错了」混成一个（E-0021 同族）；调用方先过 `numberConfigPrecheckError`，
 * 本函数只对「合法或空」负责。
 */
export function readNumberConfig(root: HTMLElement): NumberConfigInput {
  const rawPrefix = root.querySelector<HTMLInputElement>(`#${NUMBER_PREFIX_INPUT_ID}`)?.value ?? ''
  const rawStart = root.querySelector<HTMLInputElement>(`#${NUMBER_START_INPUT_ID}`)?.value ?? ''
  const start = rawStart.trim() === '' ? null : Number(rawStart.trim())
  return {
    number_prefix: rawPrefix.trim(),
    number_start: start !== null && Number.isInteger(start) ? start : null,
  }
}

/** 编号两栏的体验层预检（文案带字段名；服务端 §2.1 还会再判一次）。 */
export function numberConfigPrecheckError(root: HTMLElement): string {
  const { number_prefix, number_start } = readNumberConfig(root)
  if (number_prefix !== '' && !NUMBER_PREFIX_RE.test(number_prefix)) {
    return '编号前缀只能是 1~5 个英文字母'
  }
  if (number_start !== null && (!Number.isInteger(number_start) || number_start < 1)) {
    return '起始编号必须是 ≥ 1 的整数'
  }
  return ''
}

/** 注册链接的 m 端路径（spec §4：`location.origin + '/m/register?tk=' + token`）。 */
export const REGISTER_PAGE_PATH = '/m/register?tk='

/**
 * 状态 → **色标类 + 色点色值**（spec §4：「状态列用色点/文字区分」）。
 *
 * ⚠️ 一对值只在这一处定义（色标类与色点色值同源）：两处各抄一份色值就是
 * E-0021 的「同一事实两套判据」。⚠️ 色值**当场从 vendored `public/layui/css/layui.css`
 * 取的**，⛔ 不凭记忆：`.layui-bg-green{background-color:#16baaa}`、
 * `.layui-bg-orange{background-color:#ffb800}`、`.layui-bg-gray{background-color:#fafafa;
 * color:#5f5f5f}`——灰色那一档 badge 自带浅底，色点用它的**文字色** `#5f5f5f`
 * （同一个类里的同源值，保证色点与色标读起来是同一种"灰"）。
 * `__tests__/self-registration.spec.ts` 里有一条把它们与该文件对拍的判据（漂了就红）。
 */
export const STATUS_STYLE: Record<SelfRegInviteStatus, { badge: string; color: string; label: string }> = {
  unused: { badge: 'layui-badge layui-bg-green', color: '#16baaa', label: '未使用' },
  used: { badge: 'layui-badge layui-bg-gray', color: '#5f5f5f', label: '已使用' },
  expired: { badge: 'layui-badge layui-bg-orange', color: '#ffb800', label: '已过期' },
}

/**
 * 页内用户可见文案（抽成常量只为让 spec 钉住字面值）。
 *
 * ⚠️ 错误提示**带字段名**（E-0074 口诀：「不带字段名的错误文案基本都是第三方表单库的
 * 内置消息」）；成功文案照 spec §4 的原话（「链接已复制，7 天内有效，注册一人后自动失效」）。
 */
export const MESSAGES = {
  /** 配置卡：一个游戏都没勾就点保存（体验层预检；服务端还会再判一次，spec §3.1）。 */
  noGames: '请先勾选至少一款注册游戏',
  /** 配置保存成功。 */
  saved: '注册配置已保存',
  /** 链接生成并复制成功（spec §4 原话）。 */
  linkCopied: '链接已复制，7 天内有效，注册一人后自动失效',
  /** 生成成功但复制失败：完整链接已经显示在页面上，让用户手动复制，⛔ 不算失败。 */
  copyFailed: '链接已生成，但自动复制失败，请在下方手动复制',
  /** 记录列表为空的空态（空态 ≠ 取数失败）。 */
  empty: '还没有生成过注册链接',
  /** 作废两跳 confirm 的问句（照 system-user-list 删除用户的「两次确认」形状）。 */
  revokeConfirmFirst: '作废后这条链接立即失效，持有链接的人将无法注册。确定作废吗？',
  revokeConfirmSecond: '再次确认：作废无法恢复，需要时只能重新生成一条链接。确定作废吗？',
  /** 作废成功的提示（confirm 弹窗的标题也用它那一族文案）。 */
  revoked: '已作废，链接立即失效',
  /** 兜底失败文案（后端文案通常已经有了）。 */
  failed: '操作失败',
} as const
/** ISO 8601 → `2026-10-16 12:00:00`（只截到秒；时区偏移留在原地，不做时区换算）。 */
export function formatTime(value: string): string {
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/** 空值那一格的占位（与 feedback 页同一颗字符；空白会被读成"这列没数据"）。 */
const EMPTY_CELL = '<span style="color:#999">—</span>'

/**
 * 渲染游戏勾选框（照 `user-adduser.ts::gamesMarkup` 的形状：`value` 放 **id**、
 * `title` 放名字——`value` 不可见，`title` 决定渲染出来的标签文案，于是可见结果与
 * 对标同形，而选中值是我们能用的 id）。
 * ⚠️ 游戏名来自服务端且进 `innerHTML` ⇒ 一律过 `escapeHtml`（存储型 XSS 的典型入口）。
 */
export function gamesMarkup(games: readonly ConfigRow[], checked: readonly number[]): string {
  const chosen = new Set(checked)
  return games
    .map(
      (g) =>
        `<div class="layui-inline" style="margin-right:5px; margin-top:0px">` +
        `<input type="checkbox" name="${GAMES_INPUT_NAME}" lay-skin="tag" value="${g.id}" title="${escapeHtml(g.name)}"${chosen.has(g.id) ? ' checked' : ''} />` +
        `</div>`,
    )
    .join('')
}

/** 勾选的游戏（照 `user-adduser.ts::readCheckedGames` 的形状：`:checked` 伪类读 id）。 */
export function readCheckedGames(root: HTMLElement): number[] {
  return Array.from(
    root.querySelectorAll<HTMLInputElement>(`input[name="${GAMES_INPUT_NAME}"]:checked`),
  )
    .map((el) => Number(el.value))
    .filter((v) => Number.isInteger(v) && v > 0)
}

/**
 * 开关的当前态（layui 渲染后把状态同步回**原生 input 的 `.checked`**——所以读 property）。
 * ⚠️ 返回**真布尔**：`?.checked === true`，缺节点按「关」读，⛔ 不是真值陷阱那类写法
 * （这一格是「读层已归一化成 bool」的形态，调用方按真值判断）。
 */
export function readForbidDefault(root: HTMLElement): boolean {
  return (
    root.querySelector<HTMLInputElement>(`input[name="${FORBID_SWITCH_NAME}"]`)?.checked === true
  )
}

/**
 * 把文本写进剪贴板（照 `user-adduser.ts::copyToClipboard` 的形状，有意不 import 它——
 * 见文件头）。两者都异步/可能被拒 ⇒ 返回 Promise，调用方据此选文案。
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  const legacy = (): boolean => {
    const el = document.createElement('textarea')
    el.value = text
    // 放到视口外但仍可选中：`display:none` 会让 execCommand 失败
    el.setAttribute('readonly', '')
    el.style.position = 'fixed'
    el.style.top = '-1000px'
    el.style.opacity = '0'
    document.body.appendChild(el)
    el.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.body.removeChild(el)
    return ok
  }

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 非安全上下文/无权限 ⇒ 落到 execCommand
    }
  }
  return legacy()
}

/**
 * 生成的链接那一格（完整链接 + 有效期至）。
 *
 * ⚠️ `token` 经由 `url` 进 `innerHTML` ⇒ **转义后**放（令牌虽是自己生成的，但这一格是
 * `innerHTML` 通道，转义是本仓纪律、不按数据来源豁免）。
 */
export function linkHtml(url: string, expiresAt: string): string {
  return (
    `<div style="margin-top:8px;word-break:break-all">` +
    `<span style="color:#666">注册链接（${formatTime(expiresAt)} 前有效）：</span>` +
    `<span class="selfRegLinkText" style="color:#01aaed">${escapeHtml(url)}</span>` +
    `</div>`
  )
}

/** 表头那一行（与 `rowHtml` 的列**同一份清单**，两处各写一份会漂移——以这一份为准）。 */
export const LIST_HEADS: readonly string[] = [
  '生成时间',
  '生成人（开户人）',
  '状态',
  '有效期至',
  '注册进来的人',
  '注册时间',
  '勾选游戏',
  '操作',
]

/**
 * 记录表的**一行**（纯函数）。
 *
 * ⚠️ 每个来自服务端的字段**一律过 `escapeHtml`**（`created_by_name` / `consumed_by_name` /
 * 游戏名都是可变的文本，而这一段走 `innerHTML`）。
 * ⚠️ 「注册进来的人 / 注册时间」在**未使用**行是 `null` ⇒ 渲染「—」而不是空白
 * （空结果不等于没有数据，`AGENTS.md` §5-E）；「勾选游戏」**未使用**显示「—」（spec §4 原话），
 * 已使用但没勾游戏的行同样落到「—」（`chosen_game_names` 为空）。
 * ⚠️ 「操作」列（D-0253）：**只有未使用行**画「作废」按钮——已消费的行没有可作废的东西，
 * 已过期（含已作废）的行同样没有；两类都落「—」。按钮不带跳转语义，`type="button"`。
 */
export function rowHtml(row: SelfRegInviteRow): string {
  const style = STATUS_STYLE[row.status]
  const games =
    row.chosen_game_names.length > 0
      ? escapeHtml(row.chosen_game_names.join('、'))
      : EMPTY_CELL
  const revokeCell =
    row.status === 'unused'
      ? `<button type="button" class="${REVOKE_BUTTON_CLASS}" data-selfreg-revoke="${row.id}">作废</button>`
      : EMPTY_CELL
  return (
    `<tr class="${ROW_CLASS}" data-selfreg-row="${row.id}">` +
    `<td>${formatTime(row.created_at)}</td>` +
    `<td>${escapeHtml(row.created_by_name)}</td>` +
    `<td><span class="${style.badge}">${style.label}</span></td>` +
    `<td>${formatTime(row.expires_at)}</td>` +
    `<td>${row.consumed_by_name === null ? EMPTY_CELL : escapeHtml(row.consumed_by_name)}</td>` +
    `<td>${row.consumed_at === null ? EMPTY_CELL : formatTime(row.consumed_at)}</td>` +
    `<td>${games}</td>` +
    `<td>${revokeCell}</td>` +
    `</tr>`
  )
}

/** 整个记录表的行标记（纯函数）。⚠️ 空态要说出来，⛔ 不是留一块空白。 */
export function listHtml(rows: readonly SelfRegInviteRow[]): string {
  if (rows.length === 0) {
    return (
      `<tr><td colspan="${LIST_HEADS.length}" style="text-align:center;color:#999">` +
      `${MESSAGES.empty}</td></tr>`
    )
  }
  return rows.map((row) => rowHtml(row)).join('')
}

/**
 * 页面标记（三卡）。
 *
 * ⚠️ 配置卡那一段是 **`.layui-form`**：`form.render(type, filter)` 的根选择器就是
 * `.layui-form`（vendored layui 的 `h=".layui-form"`）⇒ 少了它，`lay-skin` 的裸 input
 * 不会被渲染。⚠️ 本页**没有** `lay-submit` 的表单提交语义：「保存配置」是普通按钮，
 * 由本文件自己挂 `click`（载荷形状是我们自己的）。
 */
export function buildHtml(): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div style="font-size:14px;font-weight:700;margin:2px 0 8px">注册配置</div>
  <div class="${FORM_CLASS} layui-form" lay-filter="${FORM_FILTER}">
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:auto">注册游戏</label>
      <div class="layui-input-block" id="${GAMES_BOX_ID}"></div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:auto">默认开关</label>
      <div class="layui-input-block" style="line-height:36px">
        <input type="checkbox" id="${FORBID_SWITCH_ID}" name="${FORBID_SWITCH_NAME}" lay-skin="switch" lay-text="开启|关闭">
        <span style="color:#666;margin-left:8px">新注册账号默认开启『禁自助接单』</span>
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:auto">编号前缀（字母）</label>
      <div class="layui-input-inline" style="width:120px">
        <input type="text" id="${NUMBER_PREFIX_INPUT_ID}" maxlength="5" autocomplete="off" placeholder="如 sq" class="layui-input" style="display:inline-block;width:120px">
      </div>
      <label class="layui-form-label" style="width:auto">起始编号</label>
      <div class="layui-input-inline" style="width:120px">
        <input type="number" id="${NUMBER_START_INPUT_ID}" min="1" step="1" autocomplete="off" placeholder="如 20001" class="layui-input" style="display:inline-block;width:120px">
      </div>
      <span style="color:#999;margin-left:8px">两栏都填了才启用编号（账号名称 = 前缀+编号，如 sq20001）；留空则用注册人填的支付宝姓名</span>
    </div>
    <div class="layui-form-item">
      <button type="button" class="layui-btn" id="${SAVE_BUTTON_ID}">保存配置</button>
      <span style="color:#999;margin-left:8px">生成链接时按保存那一刻的配置固化（游戏池与开关都是快照）</span>
    </div>
  </div>

  <div class="${INVITE_CLASS}" style="margin-top:16px">
    <div style="font-size:14px;font-weight:700;margin:0 0 8px">生成注册链接</div>
    <div style="color:#666;margin-bottom:8px">链接一次性 · 7 天有效 · 按保存时的配置固化；注册一人后自动失效</div>
    <button type="button" class="layui-btn layui-btn-normal" id="${GENERATE_BUTTON_ID}">生成并复制链接</button>
    <div id="${LINK_BOX_ID}"></div>
  </div>

  <div class="${LIST_CLASS}" style="margin-top:16px">
    <div style="margin-bottom:8px">
      <span style="font-size:14px;font-weight:700">链接与注册记录</span>
      <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${REFRESH_BUTTON_ID}" style="margin-left:8px">刷新</button>
    </div>
    <table class="layui-table" style="margin:0">
      <thead><tr id="${LIST_HEAD_ID}"></tr></thead>
      <tbody id="${LIST_BODY_ID}"></tbody>
    </table>
  </div>
</div>`
}

/** 无权限时的自检提示（页面被深链打开时用；与路由那道门同源，⛔ 不是第二套判据）。 */
export const NO_ACCESS_MESSAGE = '无权限查看自助注册'

export const __testables = {
  buildHtml,
  copyToClipboard,
  formatTime,
  gamesMarkup,
  linkHtml,
  listHeadHtml: (): string => LIST_HEADS.map((h) => `<th>${h}</th>`).join(''),
  listHtml,
  numberConfigPrecheckError,
  readCheckedGames,
  readForbidDefault,
  readNumberConfig,
  rowHtml,
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['form', 'layer'])
    const form = layui.form
    const layer = layui.layer as LayuiModule
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /**
     * ⚠️ **本页自己的门禁**：iframe 可被深链（`page.html?page=self-registration`）打开，
     * 那一刻路由的 `meta.permissions` 不生效（先例与理由：`pages/feedback.ts` 的同名自检）。
     * ⚠️ 注入标记必须排在门禁**之后**：`layui-island/page.ts` 丢掉 `mount` 的返回值，
     * 拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的深链看到整个页面壳。
     */
    const me = await fetchMe()
    if (!canNode(me, SELF_REG_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    let saving = false
    let generating = false

    const gamesBox = root.querySelector<HTMLElement>(`#${GAMES_BOX_ID}`)
    const switchEl = root.querySelector<HTMLInputElement>(`#${FORBID_SWITCH_ID}`)
    const linkBox = root.querySelector<HTMLElement>(`#${LINK_BOX_ID}`)
    const listBody = root.querySelector<HTMLElement>(`#${LIST_BODY_ID}`)
    const listHead = root.querySelector<HTMLElement>(`#${LIST_HEAD_ID}`)

    /**
     * 把配置回填进控件（**预填走原生 `checked` 属性**，`form.render()` 读的是它——
     * `user-adduser.ts` 同一条纪律：`.checked = true` 后再 render 会被打回原值）。
     */
    function applyConfigToDom(config: SelfRegConfig): void {
      if (gamesBox) gamesBox.innerHTML = gamesMarkup(gamesCache, config.game_ids)
      if (switchEl) {
        if (config.forbid_orders_default) switchEl.setAttribute('checked', '')
        else switchEl.removeAttribute('checked')
      }
      // 编号两栏（spec §2.1）：GET 回什么填什么（没配 = 空串 / 空 input，⛔ 不编默认值）。
      const prefixEl = root.querySelector<HTMLInputElement>(`#${NUMBER_PREFIX_INPUT_ID}`)
      const startEl = root.querySelector<HTMLInputElement>(`#${NUMBER_START_INPUT_ID}`)
      if (prefixEl) prefixEl.value = config.number_prefix ?? ''
      if (startEl) startEl.value = config.number_start === null || config.number_start === undefined ? '' : String(config.number_start)
    }

    /** 取记录并铺开（唯一的取数出口；表头与空态都从这里走）。 */
    async function reload(): Promise<void> {
      const rows = await listSelfRegInvites()
      if (listHead) listHead.innerHTML = __testables.listHeadHtml()
      if (listBody) listBody.innerHTML = listHtml(rows)
    }

    // 首屏：先铺表头（空列表时也要有列名，⛔ 不是一片白），配置与游戏并行取。
    if (listHead) listHead.innerHTML = __testables.listHeadHtml()

    /** 游戏全量字典（配置卡控件的数据源；fetchAllGames 的理由见文件头）。 */
    let gamesCache: ConfigRow[] = []
    const [config, games] = await Promise.all([
      fetchSelfRegConfig().catch((e: unknown) => {
        msg(e instanceof Error ? e.message : MESSAGES.failed)
        return null
      }),
      fetchAllGames().catch((e: unknown) => {
        msg(e instanceof Error ? e.message : MESSAGES.failed)
        return [] as ConfigRow[]
      }),
    ])
    gamesCache = games
    if (config) applyConfigToDom(config)
    // 所有控件就位后一次性渲染（layui 把 tag checkbox / switch 换成自己的 DOM）。
    // ⚠️ layui 的 `form.render(type, filter)` 习惯用 `null` 表示"全部类型"（user-adduser 同款），
    //    签名收窄写成 `string | null` 而不是塞 `undefined`——两种在 layui 里语义不同。
    ;(form.render as (t?: string | null, f?: string) => void)(null, FORM_FILTER)

    try {
      await reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    /** 保存配置（spec §4 卡 1）。⚠️ 空池的**服务端**判据在 §3.1；这里只挡「一个都没勾」与编号两栏的明显非法值。 */
    async function saveConfig(): Promise<void> {
      if (saving) return
      const gameIds = readCheckedGames(root)
      if (gameIds.length === 0) {
        msg(MESSAGES.noGames)
        return
      }
      const numberProblem = numberConfigPrecheckError(root)
      if (numberProblem !== '') {
        msg(numberProblem)
        return
      }
      saving = true
      try {
        await saveSelfRegConfig({
          game_ids: gameIds,
          forbid_orders_default: readForbidDefault(root),
          ...readNumberConfig(root),
        })
        msg(MESSAGES.saved)
      } catch (e) {
        msg(e instanceof Error ? e.message : MESSAGES.failed)
      } finally {
        saving = false
      }
    }

    /**
     * 生成并复制链接（spec §4 卡 2）。
     *
     * ⚠️ **复制失败不算生成失败**：链接已经在响应里，页面上**必须**把完整链接显示出来
     * （`linkBox`），提示改成「请在下方手动复制」——静默丢掉唯一一份明文链接，
     * 用户就得再生成一条；一次性按**注册**算，多余链接只是一条没消费的登记行，纯噪音。
     */
    async function generate(): Promise<void> {
      if (generating) return
      generating = true
      try {
        const created = await createSelfRegInvite()
        const url = `${location.origin}${REGISTER_PAGE_PATH}${created.token}`
        const copied = await copyToClipboard(url)
        if (linkBox) linkBox.innerHTML = linkHtml(url, created.expires_at)
        msg(copied ? MESSAGES.linkCopied : MESSAGES.copyFailed)
      } catch (e) {
        msg(e instanceof Error ? e.message : MESSAGES.failed)
      } finally {
        generating = false
      }
    }

    /**
     * 作废一条链接（D-0253，spec 卡 3 的「作废」动作）。
     *
     * ⚠️ **两跳 confirm 照 `system-user-list.ts` 删除用户的既有形状**：两连问**都**确认
     * 才发请求；任一跳取消（confirm 的回调不被调用）就什么都不发。
     * ⚠️ 成功后**必须刷新列表**：这一行的状态落地为「已过期」，且操作列的按钮随之消失
     * （只有未使用行画作废按钮）。
     * ⚠️ 失败如实透传后端文案（已消费 / 已过期各有明确的一句，D-0253 ②）。
     */
    async function revoke(id: number): Promise<void> {
      ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
        MESSAGES.revokeConfirmFirst,
        { title: '作废链接' },
        (firstIndex: number) => {
          ;(layer.close as (i: number) => void)(firstIndex)
          ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
            MESSAGES.revokeConfirmSecond,
            { title: '作废链接' },
            (secondIndex: number) => {
              ;(layer.close as (i: number) => void)(secondIndex)
              void (async () => {
                try {
                  await revokeSelfRegInvite(id)
                  msg(MESSAGES.revoked)
                } catch (e) {
                  msg(e instanceof Error ? e.message : MESSAGES.failed)
                }
                try {
                  await reload()
                } catch (e) {
                  msg(e instanceof Error ? e.message : MESSAGES.failed)
                }
              })()
            },
          )
        },
      )
    }

    root.querySelector<HTMLButtonElement>(`#${SAVE_BUTTON_ID}`)?.addEventListener('click', () => {
      void saveConfig()
    })
    root.querySelector<HTMLButtonElement>(`#${GENERATE_BUTTON_ID}`)?.addEventListener('click', () => {
      void generate()
    })
    root.querySelector<HTMLButtonElement>(`#${REFRESH_BUTTON_ID}`)?.addEventListener('click', () => {
      void reload().catch((e: unknown) => {
        msg(e instanceof Error ? e.message : MESSAGES.failed)
      })
    })
    // 「作废」走**事件委托**（行由 `reload()` 重铺 innerHTML，逐行挂监听会随重铺丢掉）；
    // 按钮 id 从行容器的 `data-selfreg-row` 取（与 `rowHtml` 写进去的是同一个值）。
    listBody?.addEventListener('click', (event: MouseEvent) => {
      const button = (event.target as HTMLElement | null)?.closest(REVOKE_BUTTON_SELECTOR)
      if (!button) return
      const rowId = Number(button.closest(`.${ROW_CLASS}`)?.getAttribute('data-selfreg-row'))
      if (Number.isInteger(rowId) && rowId > 0) void revoke(rowId)
    })

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
