/**
 * 【第三方平台】→「QQ机器人设置」页（island key = `qqbot-settings`，路由 `/thirdparty/qqbot-settings`）。
 *
 * 契约的另一半是**实现即契约**：`src/apps/qqbot/urls.py` + `api/views.py` +
 * `api/serializers.py`；端点封装在 `api/qqbot.ts`（文件头有逐条契约表）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里没有机器人对接这一页 ⇒ 本页**没有证据件**、不走 `mountConfigLike`，
 * 标记由本文件自己写，骨架照 `pages/wecom-group-qr.ts`（同类**自有功能页**）。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ## 五个区块（单页纵向分区）
 *
 * 1. **机器人密钥**：AppID、AppSecret（password 输入；**留空 = 不改**，后端把空串当「不动它」
 *    ——⛔ 那一格不是「清掉」）、已认证开关（限频 60/分钟）、启用开关（灰字提示：改凭据 /
 *    认证状态后需重启 `qqbot_ws` 生效——ws 进程只在建连时读一次凭据）、保存；保存后展示
 *    **验真结果**（响应 `verified` 只在带了凭据格时出现：true 绿 / false 把 `last_error` 显示成红条）；
 * 2. **连接状态与分享链接**：connection_state / last_alive_at / last_event_at、
 *    markdown 降级黄条（`markdown_ok=false` ⇒ 已自动改发纯文本）、生成群分享链接（弹层展示 + 复制）；
 * 3. **群列表**：每群一张卡片（群名 / 成员数 / 机器人身份**为空时显示「白名单未批」占位**——
 *    那是官方白名单接口未批、资料没拿到，⛔ 不是 0）、状态徽标、备注编辑、刷新群信息
 *    （白名单未批降级进 `info_error`，如实显示）；展开区：推送配置（开关 + markdown/text +
 *    游戏**多选** + 保存）、发送测试消息、禁言列表、入群申请（本地留痕表（含**操作人**列，
 *    空 = 自动）+ remote 块 + 通过/拒绝 + 审批策略与白名单编辑（名单由 `loadGroups` 用群行回填——刷新后不再丢））；
 * 4. **全局自定义菜单**：编辑器（一级 ≤10 行；type 下拉 switch/send_message/link/menu；
 *    子菜单 ≤5 且只许 send_message/link；link 前端也做 https 校验——后端仍是最终裁决者）；
 *    「读取当前菜单」（显示 version）与「保存到 QQ」；
 * 5. **推送记录**：发件箱分页表（时间 / 群 / 类型 / 状态徽标 / 错误码 / 内容），群与状态可筛选。
 *
 * ## 空态
 *
 * 机器人**未配置**（无 app_id 或没配过 secret）时，2–5 区各显示一句引导「先在上方保存密钥」；
 * 群列表为空时显示「还没有群」的引导文案。
 *
 * ## 四条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="layui-card ${CARD_CLASS}"` 拼标记、`CARD_SELECTOR` 查节点
 *    ——写成 `class="${CARD_SELECTOR}"` 会造出 `class=".qqbotCard"`、永远匹配不到）；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记初始化）；
 * ④ 页里有定时器时：假定时器要先 `vi.useFakeTimers()` 再包装，且每个用例自己收口
 *    （⚠️ 本页**没有**任何定时器：推送记录是筛选/翻页时拉一次）。
 *
 * ## ⛔ 本文件里不写带点的字面量（`tests/test_rbac_menu_alignment.py` 的岛页扫描）
 *
 * 节点码常量住在 **api 模块**（`QQBOT_SETTINGS_NODE`，先例：`taker-apps` / `open-apps`），
 * 本文件只 import；同理 CSS 选择器一律用 `.类名` / `#id` 形态（⛔ 不写 `button.xxx` 这种
 * 「标签名.类名」选择器——那个形状会被扫描当成权限点、判据 1 当场红）。
 */
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { fetchMe } from '@/api/auth'
import {
  QQBOT_SETTINGS_NODE,
  approveQQBotJoinRequest,
  createQQBotShareLink,
  getQQBotBinding,
  getQQBotMenu,
  listQQBotGroupJoinRequests,
  listQQBotGroupMutes,
  listQQBotGroups,
  listQQBotMessages,
  putQQBotGroupPushConfig,
  putQQBotMenu,
  testQQBotGroupPush,
  updateQQBotBinding,
  updateQQBotGroupJoinPolicy,
  updateQQBotGroupRemark,
  type QQBotBinding,
  type QQBotBindingSaveResult,
  type QQBotGroupRow,
  type QQBotGroupsPayload,
  type QQBotJoinPolicyMode,
  type QQBotJoinRequestsPayload,
  type QQBotJoinRequestRow,
  type QQBotMutesPayload,
  type QQBotMessageRow,
  type QQBotPushConfigSummary,
  type QQBotRemoteError,
} from '@/api/qqbot'
import { escapeHtml } from '@/layui-island/escape'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'qqbot-settings'
export const TITLE = 'QQ机器人设置'

// ── 类名与选择器**分成两个常量**（陷阱 ①）─────────────────────────────────────

/** 每一个区块的外层卡片。 */
export const CARD_CLASS = 'qqbotCard'
export const CARD_SELECTOR = `.${CARD_CLASS}`
/** 「机器人没配置」时的引导块（2–5 区各一个；配置好了就藏起来）。 */
export const EMPTY_HINT_CLASS = 'qqbotEmptyHint'
export const EMPTY_HINT_SELECTOR = `.${EMPTY_HINT_CLASS}`
/** 「配置好了才显示」的内容块（与 EMPTY_HINT 成对）。 */
export const CONFIGURED_ONLY_CLASS = 'qqbotNeedsBot'
export const CONFIGURED_ONLY_SELECTOR = `.${CONFIGURED_ONLY_CLASS}`

// ── 区块 1：机器人密钥 ──────────────────────────────────────────────────────

export const APP_ID_INPUT_ID = 'qqbotAppId'
export const SECRET_INPUT_ID = 'qqbotAppSecret'
export const BINDING_RESULT_ID = 'qqbotBindingResult'
export const BINDING_SAVE_BUTTON_ID = 'qqbotBindingSaveBtn'
/** 两个开关的 `layui-form` 容器（`id` 与 `lay-filter` 同值；`form.render` 按 `.layui-form` 找根）。 */
export const BINDING_FORM_ID = 'qqbotBindingForm'
export const BINDING_FORM_FILTER = 'qqbotBindingForm'
export const AUTH_SWITCH_ID = 'qqbotAuthSwitch'
export const AUTH_SWITCH_FILTER = 'qqbotAuthSwitch'
export const AUTH_SWITCH_NAME = 'qqbotAuthSwitch'
export const ENABLED_SWITCH_ID = 'qqbotEnabledSwitch'
export const ENABLED_SWITCH_FILTER = 'qqbotEnabledSwitch'
export const ENABLED_SWITCH_NAME = 'qqbotEnabledSwitch'

// ── 区块 2：连接状态与分享链接 ───────────────────────────────────────────────

export const BOT_NAME_TEXT_ID = 'qqbotBotName'
export const CONN_STATE_TEXT_ID = 'qqbotConnState'
export const LAST_ALIVE_TEXT_ID = 'qqbotLastAlive'
export const LAST_EVENT_TEXT_ID = 'qqbotLastEvent'
export const MARKDOWN_WARN_ID = 'qqbotMarkdownWarn'
export const SHARE_GROUP_SELECT_ID = 'qqbotShareGroup'
export const SHARE_BUTTON_ID = 'qqbotShareBtn'
export const SHARE_RESULT_ID = 'qqbotShareResult'
export const SHARE_COPY_BUTTON_ID = 'qqbotShareCopyBtn'

// ── 区块 3：群列表（卡片是**动态渲染**的 ⇒ 群内控件用类常量 + 事件委托）─────────

export const GROUPS_WARN_ID = 'qqbotGroupsWarn'
export const GROUPS_BODY_ID = 'qqbotGroupsBody'
export const GROUP_CARD_CLASS = 'qqbotGroupCard'
export const GROUP_TOGGLE_CLASS = 'qqbotGroupToggle'
export const GROUP_TOGGLE_CLOSED_TEXT = '群设置（推送 / 测试 / 禁言 / 入群申请）— 点这里展开'
export const GROUP_TOGGLE_OPEN_TEXT = '群设置（推送 / 测试 / 禁言 / 入群申请）— 点这里收起'
export const GROUP_DETAIL_CLASS = 'qqbotGroupDetail'
export const REMARK_INPUT_CLASS = 'qqbotRemarkInput'
export const REMARK_SAVE_CLASS = 'qqbotRemarkSave'
export const REFRESH_INFO_CLASS = 'qqbotRefreshInfo'
export const PUSH_ACTIVE_CLASS = 'qqbotPushActive'
/** msg_type 单选的 `name` 前缀（每群一个 radio 组：`qqbotPushType-<群id>`，⛔ 不共用免得串组）。 */
export const PUSH_TYPE_PREFIX = 'qqbotPushType'
export const GAME_CHECK_CLASS = 'qqbotGameCheck'
export const PUSH_SAVE_CLASS = 'qqbotPushSave'
export const TEST_PUSH_CLASS = 'qqbotTestPush'
export const MUTES_TOGGLE_CLASS = 'qqbotMutesToggle'
export const MUTES_BODY_CLASS = 'qqbotMutesBody'
export const JR_TOGGLE_CLASS = 'qqbotJrToggle'
export const JR_BODY_CLASS = 'qqbotJrBody'
export const POLICY_SELECT_CLASS = 'qqbotPolicySelect'
export const WHITELIST_INPUT_CLASS = 'qqbotWhitelistInput'
export const POLICY_SAVE_CLASS = 'qqbotPolicySave'
export const JR_APPROVE_CLASS = 'qqbotJrApprove'
export const JR_DECLINE_CLASS = 'qqbotJrDecline'

// ── 区块 4：全局自定义菜单 ──────────────────────────────────────────────────

export const MENU_GROUP_SELECT_ID = 'qqbotMenuGroup'
export const MENU_LOAD_BUTTON_ID = 'qqbotMenuLoadBtn'
export const MENU_VERSION_TEXT_ID = 'qqbotMenuVersion'
export const MENU_ROWS_ID = 'qqbotMenuRows'
export const MENU_ADD_BUTTON_ID = 'qqbotMenuAddBtn'
export const MENU_SAVE_BUTTON_ID = 'qqbotMenuSaveBtn'
export const MENU_MSG_ID = 'qqbotMenuMsg'
export const MENU_ROW_CLASS = 'qqbotMenuRow'
export const MENU_ROW_NAME_CLASS = 'qqbotMenuName'
export const MENU_ROW_TYPE_CLASS = 'qqbotMenuType'
export const MENU_ROW_VALUE_CLASS = 'qqbotMenuValue'
export const MENU_ROW_ADDSUB_CLASS = 'qqbotMenuAddSub'
export const MENU_ROW_DEL_CLASS = 'qqbotMenuDel'
export const MENU_SUB_ROW_CLASS = 'qqbotMenuSubRow'
export const MENU_SUB_NAME_CLASS = 'qqbotMenuSubName'
export const MENU_SUB_TYPE_CLASS = 'qqbotMenuSubType'
export const MENU_SUB_VALUE_CLASS = 'qqbotMenuSubValue'
export const MENU_SUB_DEL_CLASS = 'qqbotMenuSubDel'

// ── 区块 5：推送记录 ────────────────────────────────────────────────────────

export const MSG_GROUP_FILTER_ID = 'qqbotMsgGroup'
export const MSG_STATUS_FILTER_ID = 'qqbotMsgStatus'
export const MSG_RELOAD_BUTTON_ID = 'qqbotMsgReloadBtn'
export const MSG_BODY_ID = 'qqbotMsgBody'
export const MSG_PAGE_INFO_ID = 'qqbotMsgPageInfo'
export const MSG_PREV_BUTTON_ID = 'qqbotMsgPrevBtn'
export const MSG_NEXT_BUTTON_ID = 'qqbotMsgNextBtn'

/** 发件箱列表的页大小（后端 `views.MESSAGE_PAGE_SIZE` 定死 20，前端照用）。 */
export const MESSAGE_PAGE_SIZE = 20

/** 一级菜单可选的类型（switch / send_message / link / menu；menu = 带子菜单的文件夹）。 */
export const MENU_TOP_TYPES = ['switch', 'send_message', 'link', 'menu'] as const
/** 二级菜单只许这两种（后端 `QQBotMenuSerializer.SUB_BUTTON_TYPES` 同值）。 */
export const MENU_SUB_TYPES = ['send_message', 'link'] as const
export type MenuTopType = (typeof MENU_TOP_TYPES)[number]
export type MenuSubType = (typeof MENU_SUB_TYPES)[number]

/** 菜单编辑器的一行（一级）。`link` 只在 type=link 时有意义；`subs` 非空 = 文件夹。 */
export type MenuSubRow = { name: string; type: MenuSubType; link: string }
export type MenuTopRow = { name: string; type: MenuTopType; link: string; subs: MenuSubRow[] }

/** 官方菜单行数上限（后端校验同值；前端先拦一次，后端仍是最终裁决者）。 */
export const MENU_TOP_LIMIT = 10
export const MENU_SUB_LIMIT = 5

// ── 用户可见文案 ────────────────────────────────────────────────────────────

/** 2–5 区在「机器人没配置」时的引导（每区各一条，说的是**这个区**该先做什么）。 */
export const EMPTY_HINT_TEXT = '先在上方保存密钥（AppID 与 AppSecret），保存通过后这里才能取数。'

export const NO_ACCESS_MESSAGE = '无权限管理 QQ 机器人设置'

export const MESSAGES = {
  failed: '操作失败',
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中链接复制',
  saved: '已保存',
  remarkSaved: '备注已保存',
  infoRefreshed: '群资料已刷新',
  pushSaved: '推送配置已保存',
  testQueued: (id: number, status: string): string =>
    `测试消息已入队（发件箱记录 #${id}，当前状态 ${status}；发没发出去看下面的推送记录）`,
  policySaved: '审批策略已保存',
  joinApproved: '已通过',
  joinDeclined: '已拒绝',
  menuPickGroup: '先选一个群',
  menuSaved: (version: number | null): string =>
    version === null ? '菜单已保存（官方没回版本号）' : `菜单已保存，版本 ${version}`,
  menuLoadFail: '读取菜单失败',
} as const

/** 「白名单未批」占位（群资料没拿到时的显示；⛔ 不是 0、不是空白）。 */
export const WHITELIST_PENDING_TEXT = '白名单未批'

/** 群列表为空的引导。 */
export const EMPTY_GROUPS_TEXT =
  '还没有群：机器人被拉进某个群、收到第一条消息后，群会出现在这里。'

/**
 * 一句「为空就显示占位」的纯函数（群名 / 机器人身份共用）。
 * ⚠️ 判据是**空串**（后端没拿到资料时就是 `""`），不是真值——`0` 之类的值不会被它吃掉。
 */
export function orWhitelistPending(value: string): string {
  return value === '' ? WHITELIST_PENDING_TEXT : value
}

/** 成员数：`null` = 资料没拿到 ⇒ 占位（⛔ 不是 0；0 是合法边界，E-0021）。 */
export function memberNumText(value: number | null): string {
  return value === null ? WHITELIST_PENDING_TEXT : String(value)
}

/** 一个 ISO 8601 时刻的显示形态（同 `wecom-group-qr.ts::timeText`：截到秒、不做时区换算）。 */
export function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/** 群状态徽标（判据打**机器值**；`active` 绿 / `gone` 红）。 */
export function statusBadgeHtml(status: string): string {
  const color = status === 'active' ? '#009688' : status === 'gone' ? '#FF5722' : '#999'
  return `<span style="color:${color};font-weight:700">${escapeHtml(status)}</span>`
}

/** 推送配置摘要的一句话（`null` = 还没配过）。 */
export function pushSummaryText(config: QQBotPushConfigSummary | null): string {
  if (config === null) return '推送：未配置'
  const on = config.is_active ? '已启用' : '已停用'
  return `推送${on} · ${config.msg_type} · 限定游戏 ${config.game_ids.length} 个`
}

/** 远端 / 白名单降级错误的一句话（code 是官方字符串，原样带上）。 */
export function remoteErrorText(err: QQBotRemoteError): string {
  return `${err.message}（code ${err.code}）`
}

/**
 * 保存密钥之后那句回报（`verified` 只在带了凭据格时出现——三条分支对着**真实后果**说）。
 */
export function bindingSaveReport(saved: QQBotBindingSaveResult): string {
  if (saved.verified === true) {
    return '<span style="color:#009688">已保存：凭据验真通过（官方 token 换取成功）。</span>'
  }
  if (saved.verified === false) {
    return (
      '<span style="color:#FF5722">已保存，但验真失败——连接不会建立。' +
      `最近错误：${escapeHtml(saved.last_error === '' ? '（后端没有给出原因）' : saved.last_error)}` +
      '。请核对 AppID / AppSecret 后重存。</span>'
    )
  }
  return '已保存（这次没有触发验真：只改了开关）。'
}

/**
 * 「禁言列表」的标记：空列表 / 降级 / 有数据 三种都**如实**显示（⛔ 不把失败渲染成空）。
 * `items` 是官方回包原样（形状不由我们定）⇒ 非空时按 JSON 原样展示。
 */
export function mutesHtml(payload: QQBotMutesPayload): string {
  if (payload.items === null) {
    const reason = payload.remote_error ? remoteErrorText(payload.remote_error) : '远端没有返回原因'
    return `<div style="color:#FF5722;line-height:24px">禁言列表拉取失败：${escapeHtml(reason)}</div>`
  }
  if (payload.items.length === 0) {
    return '<div style="color:#666;line-height:24px">当前没有禁言中的成员。</div>'
  }
  return `<pre style="background:#f7f7f7;border:1px solid #eee;padding:8px;overflow:auto">${escapeHtml(
    JSON.stringify(payload.items, null, 2),
  )}</pre>`
}

/** 入群申请本地留痕的一行（处置按钮只给**还挂着**的那几条画——别的处置已经有结局了）。
 * 「操作人」列：没处置（`acted_at` 空）显示「—」；处置过但没有操作人
 * （机器路径的自动审批）显示「自动」；人工处置显示操作人显示名。 */
export function joinRowHtml(row: QQBotJoinRequestRow): string {
  const pending = row.decision === 'manual_pending'
  const operator =
    row.acted_at === null ? '—' : escapeHtml(row.acted_by_name ?? '自动')
  const actions = pending
    ? `<button type="button" class="layui-btn layui-btn-xs layui-btn-normal ${JR_APPROVE_CLASS}" ` +
      `data-jr-id="${encodeURIComponent(row.join_request_id)}" ` +
      `data-jr-openid="${encodeURIComponent(row.member_openid)}">通过</button> ` +
      `<button type="button" class="layui-btn layui-btn-xs layui-btn-danger ${JR_DECLINE_CLASS}" ` +
      `data-jr-id="${encodeURIComponent(row.join_request_id)}" ` +
      `data-jr-openid="${encodeURIComponent(row.member_openid)}">拒绝</button>`
    : '—'
  return (
    '<tr>' +
    `<td>${escapeHtml(row.username === '' ? row.member_openid : row.username)}</td>` +
    `<td style="white-space:nowrap">${timeText(row.apply_at)}</td>` +
    `<td style="white-space:nowrap">${escapeHtml(row.decision)}</td>` +
    `<td>${escapeHtml(row.reject_reason)}</td>` +
    `<td style="white-space:nowrap">${operator}</td>` +
    `<td>${actions}</td>` +
    '</tr>'
  )
}

/** 本地留痕表（空 = 一句文案；⛔ 不渲染成空表格）。 */
export function joinRowsHtml(items: readonly QQBotJoinRequestRow[]): string {
  if (items.length === 0) {
    return '<div style="color:#666;line-height:24px">本地还没有入群申请的留痕。</div>'
  }
  return (
    '<table class="layui-table" lay-size="sm" style="margin:0"><thead><tr>' +
    '<th>申请人</th><th style="width:150px">申请时间</th><th style="width:130px">处置结果</th>' +
    '<th>拒绝理由</th><th style="width:110px">操作人</th><th style="width:130px">操作</th>' +
    '</tr></thead><tbody>' +
    items.map(joinRowHtml).join('') +
    '</tbody></table>'
  )
}

/**
 * 入群申请区块的标记：本地留痕 + remote 块 + 策略编辑（三块并列）。
 * `remote` 是官方回包**原样**（形状不由我们定）⇒ 按 JSON 展示；`remote_error` 红字。
 * `group` 传 `undefined`（列表刷新后暂时找不到那一行）时策略编辑器按 `off` 起步。
 */
export function joinRequestsHtml(
  group: QQBotGroupRow | undefined,
  payload: QQBotJoinRequestsPayload,
  whitelist: readonly string[],
): string {
  const remote =
    payload.remote_error !== undefined
      ? `<div style="color:#FF5722;line-height:24px">远端拉取失败：${escapeHtml(
          remoteErrorText(payload.remote_error),
        )}</div>`
      : payload.remote === undefined
        ? '<div style="color:#666;line-height:24px">远端这一块没有返回。</div>'
        : `<div style="color:#666;line-height:24px">远端返回（原样）：</div><pre style="background:#f7f7f7;border:1px solid #eee;padding:8px;overflow:auto">${escapeHtml(
            JSON.stringify(payload.remote, null, 2),
          )}</pre>`
  const mode = group?.join_policy_mode ?? 'off'
  const options = POLICY_MODE_OPTIONS.map(
    ([value, label]) =>
      `<option value="${value}"${value === mode ? ' selected' : ''}>${label}</option>`,
  ).join('')
  return (
    joinRowsHtml(payload.items) +
    '<div style="border-top:1px dashed #eee;margin:10px 0 6px"></div>' +
    remote +
    '<div style="border-top:1px dashed #eee;margin:10px 0 6px"></div>' +
    '<div class="layui-form-item" style="margin-bottom:4px">' +
    '<label class="layui-form-label" style="padding-left:0">审批策略</label>' +
    `<div class="layui-input-inline"><select class="${POLICY_SELECT_CLASS}">${options}</select></div>` +
    `<textarea class="layui-textarea ${WHITELIST_INPUT_CLASS}" rows="2" ` +
    `placeholder="白名单模式的名单：一行一个成员 openid（或逗号分隔）；其它模式名单会被清空"${mode === 'whitelist' ? '' : ' disabled'}>` +
    `${escapeHtml(whitelist.join('\n'))}</textarea>` +
    '</div>' +
    `<button type="button" class="layui-btn layui-btn-sm layui-btn-normal ${POLICY_SAVE_CLASS}">保存审批策略</button>`
  )
}

/** 审批策略下拉的取值与中文（取值 = 后端 `JoinPolicyMode` 逐字；中文照模型 label）。 */
export const POLICY_MODE_OPTIONS: ReadonlyArray<readonly [string, string]> = [
  ['off', '不自动处理（留人工）'],
  ['approve_all', '全部自动通过'],
  ['decline_all', '全部自动拒绝'],
  ['whitelist', '白名单自动通过，其余拒绝'],
]

/** 白名单输入的解析：换行 / 半角与全角逗号分隔、去空白、去重（保序）。 */
export function parseWhitelistInput(text: string): string[] {
  const out: string[] = []
  for (const part of text.split(/[\n,，]/)) {
    const value = part.trim()
    if (value !== '' && !out.includes(value)) out.push(value)
  }
  return out
}

/** 发件箱一行的状态色（pending 橙 / sent 绿 / failed 红）。 */
export function messageStatusColor(status: string): string {
  return status === 'sent' ? '#009688' : status === 'failed' ? '#FF5722' : '#FF9800'
}

/** 发件箱一行（错误码为空显示 —；内容服务端已截断，这里原样转义）。 */
export function messageRowHtml(row: QQBotMessageRow): string {
  return (
    '<tr>' +
    `<td style="white-space:nowrap">${timeText(row.created_at)}</td>` +
    `<td>${escapeHtml(row.group_openid)}</td>` +
    `<td>${escapeHtml(row.kind)}</td>` +
    `<td style="white-space:nowrap"><span style="color:${messageStatusColor(row.status)};font-weight:700">` +
    `${escapeHtml(row.status)}</span></td>` +
    `<td>${row.error_code === '' ? '—' : escapeHtml(row.error_code)}</td>` +
    `<td>${escapeHtml(row.payload_text)}</td>` +
    '</tr>'
  )
}

/** 发件箱表（空 = 一句文案；⛔ 不渲染成空表格）。 */
export function messagesHtml(items: readonly QQBotMessageRow[]): string {
  if (items.length === 0) {
    return '<div style="color:#666;line-height:24px">还没有推送记录。</div>'
  }
  return (
    '<table class="layui-table" lay-size="sm" style="margin:0"><thead><tr>' +
    '<th style="width:150px">时间</th><th>群</th><th style="width:100px">类型</th>' +
    '<th style="width:80px">状态</th><th style="width:90px">错误码</th><th>内容</th>' +
    '</tr></thead><tbody>' +
    items.map(messageRowHtml).join('') +
    '</tbody></table>'
  )
}

// ── 菜单编辑器的纯函数（行数上限 / 类型白名单 / https——后端仍是最终裁决者）────────

export function emptyMenuRow(): MenuTopRow {
  return { name: '', type: 'switch', link: '', subs: [] }
}

export function emptyMenuSubRow(): MenuSubRow {
  return { name: '', type: 'send_message', link: '' }
}

/** https 的前端即时校验（与后端 `startsWith('https://')` 同一条判据；提示当场说）。 */
export function menuLinkError(link: string): string | null {
  const trimmed = link.trim()
  if (trimmed === '' || !trimmed.startsWith('https://')) {
    return 'link 类型菜单的链接必须以 https:// 开头'
  }
  return null
}

/** 保存前的**全部**问题（逐条中文；空数组 = 可以保存）。行数 / 长度上限与后端校验同值。 */
export function menuPayloadErrors(rows: readonly MenuTopRow[]): string[] {
  const errors: string[] = []
  if (rows.length > MENU_TOP_LIMIT) errors.push(`一级菜单最多 ${MENU_TOP_LIMIT} 个`)
  rows.forEach((row, i) => {
    const where = `第 ${i + 1} 个一级菜单`
    const name = row.name.trim()
    if (name === '') errors.push(`${where}缺少名称`)
    else if (name.length > 10) errors.push(`${where}名称过长（最多 10 个字符）`)
    if (row.subs.length > MENU_SUB_LIMIT) {
      errors.push(`${where}的二级菜单最多 ${MENU_SUB_LIMIT} 个`)
    }
    if (row.type === 'link' && row.subs.length === 0) {
      const linkError = menuLinkError(row.link)
      if (linkError !== null) errors.push(`${where}：${linkError}`)
    }
    row.subs.forEach((sub, j) => {
      const subName = sub.name.trim()
      if (subName === '') errors.push(`${where}的第 ${j + 1} 个二级菜单缺少名称`)
      else if (subName.length > 14) errors.push(`${where}的第 ${j + 1} 个二级菜单名称过长（最多 14 个字符）`)
      if (sub.type === 'link') {
        const subLinkError = menuLinkError(sub.link)
        if (subLinkError !== null) errors.push(`${where}的第 ${j + 1} 个二级菜单：${subLinkError}`)
      }
    })
  })
  return errors
}

/** 编辑器状态 → 官方载荷（与后端测试里那份合法载荷同形：`{name, type?, link?, sub_button?}`）。 */
export function buildMenuButtons(rows: readonly MenuTopRow[]): unknown[] {
  return rows.map((row) => {
    const button: Record<string, unknown> = { name: row.name.trim() }
    if (row.subs.length > 0) {
      button.sub_button = row.subs.map((sub) => {
        const item: Record<string, unknown> = { name: sub.name.trim(), type: sub.type }
        if (sub.type === 'link') item.link = sub.link.trim()
        return item
      })
      return button
    }
    button.type = row.type
    if (row.type === 'link') button.link = row.link.trim()
    return button
  })
}

/**
 * 官方菜单回包 → 编辑器状态。只认 `{buttons: [...]}` 这一种形状（官方口径，
 * 后端测试里那份合法载荷的反向）；其它形状回 `null`（页面如实说「解析不了」，原样另展示）。
 */
export function parseMenuButtons(menu: unknown): MenuTopRow[] | null {
  if (typeof menu !== 'object' || menu === null) return null
  const buttons = (menu as { buttons?: unknown }).buttons
  if (!Array.isArray(buttons)) return null
  return buttons.map((raw) => {
    const button = (raw ?? {}) as Record<string, unknown>
    const subs = Array.isArray(button.sub_button) ? button.sub_button : []
    return {
      name: typeof button.name === 'string' ? button.name : '',
      type: (MENU_TOP_TYPES as readonly string[]).includes(String(button.type))
        ? (String(button.type) as MenuTopType)
        : 'switch',
      link: typeof button.link === 'string' ? button.link : '',
      subs: subs.map((subRaw) => {
        const sub = (subRaw ?? {}) as Record<string, unknown>
        return {
          name: typeof sub.name === 'string' ? sub.name : '',
          type: (MENU_SUB_TYPES as readonly string[]).includes(String(sub.type))
            ? (String(sub.type) as MenuSubType)
            : 'send_message',
          link: typeof sub.link === 'string' ? sub.link : '',
        }
      }),
    }
  })
}

// ── 群卡片 ─────────────────────────────────────────────────────────────────

/** 群在某处的显示名（备注优先，其次群名，最后 openid）。 */
export function groupDisplayName(group: QQBotGroupRow): string {
  return group.remark || group.group_name || group.group_openid
}

/** 一张群卡片（含展开区整块；展开区默认收起）。游戏勾选来自 `games`（当前租户全量字典）。 */
export function groupCardHtml(group: QQBotGroupRow, games: readonly ConfigRow[]): string {
  const id = group.id
  const push = group.push_config
  const gameChecks = games
    .map((game) => {
      const checked = push !== null && push.game_ids.includes(game.id) ? ' checked' : ''
      return (
        `<label style="margin-right:12px;white-space:nowrap"><input type="checkbox" ` +
        `class="${GAME_CHECK_CLASS}" data-game-id="${game.id}"${checked}> ` +
        `${escapeHtml(game.name)}</label>`
      )
    })
    .join('')
  const msgTypeRadio = (value: string, label: string): string => {
    const name = `${PUSH_TYPE_PREFIX}-${id}`
    const checked = push === null ? value === 'markdown' : push.msg_type === value
    return (
      `<label style="margin-right:12px"><input type="radio" name="${name}" ` +
      `class="${PUSH_TYPE_PREFIX}" value="${value}"${checked ? ' checked' : ''}> ${label}</label>`
    )
  }
  return (
    `<div class="layui-card ${CARD_CLASS} ${GROUP_CARD_CLASS}" data-group-id="${id}">` +
    `<div class="layui-card-header">` +
    `<b>${escapeHtml(groupDisplayName(group))}</b> ` +
    `<span style="color:#666">openid: ${escapeHtml(group.group_openid)}</span> ` +
    statusBadgeHtml(group.status) +
    '</div><div class="layui-card-body">' +
    `<div style="line-height:24px">` +
    `群名称：${escapeHtml(orWhitelistPending(group.group_name))} ｜ ` +
    `成员数：${escapeHtml(memberNumText(group.member_num))} ｜ ` +
    `机器人身份：${escapeHtml(orWhitelistPending(group.member_role))} ｜ ` +
    `收消息设置：${escapeHtml(group.recv_msg_setting === '' ? '—' : group.recv_msg_setting)}` +
    '</div>' +
    `<div style="line-height:24px;color:#666">${escapeHtml(pushSummaryText(push))}</div>` +
    `<div style="margin:6px 0">` +
    `<input type="text" class="layui-input ${REMARK_INPUT_CLASS}" style="display:inline-block;width:220px" ` +
    `value="${escapeHtml(group.remark)}" placeholder="本地备注（只自己看得见）"> ` +
    `<button type="button" class="layui-btn layui-btn-sm ${REMARK_SAVE_CLASS}">保存备注</button> ` +
    `<button type="button" class="layui-btn layui-btn-sm layui-btn-primary ${REFRESH_INFO_CLASS}">刷新群信息</button>` +
    '</div>' +
    `<a href="javascript:;" class="${GROUP_TOGGLE_CLASS}" style="font-weight:700;color:#1E9FFF">` +
    `${GROUP_TOGGLE_CLOSED_TEXT}</a>` +
    `<div class="${GROUP_DETAIL_CLASS}" style="display:none;margin-top:8px">` +
    '<div style="font-weight:700;margin:4px 0">推送配置</div>' +
    `<label style="margin-right:12px"><input type="checkbox" class="${PUSH_ACTIVE_CLASS}"` +
    `${push === null || push.is_active ? ' checked' : ''}> 启用推送</label>` +
    msgTypeRadio('markdown', 'markdown') +
    msgTypeRadio('text', '纯文本') +
    `<div style="margin:4px 0">${gameChecks === '' ? '<span style="color:#666">（当前租户没有游戏可选）</span>' : gameChecks}</div>` +
    `<button type="button" class="layui-btn layui-btn-sm layui-btn-normal ${PUSH_SAVE_CLASS}">保存推送配置</button>` +
    `<div style="font-weight:700;margin:10px 0 4px">测试</div>` +
    `<button type="button" class="layui-btn layui-btn-sm layui-btn-warm ${TEST_PUSH_CLASS}">发送测试消息</button>` +
    `<div style="font-weight:700;margin:10px 0 4px">禁言列表</div>` +
    `<a href="javascript:;" class="${MUTES_TOGGLE_CLASS}" style="color:#1E9FFF">禁言列表（点这里展开拉取）</a>` +
    `<div class="${MUTES_BODY_CLASS}" style="display:none;margin-top:4px"></div>` +
    `<div style="font-weight:700;margin:10px 0 4px">入群申请</div>` +
    `<a href="javascript:;" class="${JR_TOGGLE_CLASS}" style="color:#1E9FFF">入群申请与审批策略（点这里展开拉取）</a>` +
    `<div class="${JR_BODY_CLASS}" style="display:none;margin-top:4px"></div>` +
    '</div></div></div>'
  )
}

/**
 * 「复制」的剪贴板出口（与 `pages/wecom-group-qr.ts` 同一份实现的两处落点——页面互不 import，
 * 先例同源；缺 `navigator.clipboard` 不是静默失败）。
 */
export function copyTextToClipboard(text: string, onDone: (ok: boolean) => void): void {
  const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
    typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    onDone(false)
    return
  }
  void Promise.resolve(clipboard.writeText.call(clipboard, text)).then(
    () => onDone(true),
    () => onDone(false),
  )
}

/** 三个群下拉共用的填充逻辑（保住当前已选项；`includeAll` 给筛选下拉加「全部群」）。 */
function fillGroupSelect(
  select: HTMLSelectElement | null,
  groups: readonly QQBotGroupRow[],
  placeholder: string,
  includeAll: boolean,
): void {
  if (select === null) return
  const previous = select.value
  const options: string[] = []
  if (includeAll) options.push('<option value="">全部群</option>')
  else options.push(`<option value="">${placeholder}</option>`)
  for (const group of groups) {
    options.push(
      `<option value="${group.id}">${escapeHtml(groupDisplayName(group))}</option>`,
    )
  }
  select.innerHTML = options.join('')
  if (previous !== '' && groups.some((group) => String(group.id) === previous)) {
    select.value = previous
  }
}

/** 页面标记（两个块——引导与内容——都渲染出来、由 `applyBinding` 切 `display`，理由同 wecom-group-qr）。 */
export function buildHtml(): string {
  return `
<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">机器人密钥</div>
  <div class="layui-card-body">
    <div class="layui-form" id="${BINDING_FORM_ID}" lay-filter="${BINDING_FORM_FILTER}">
      <div class="layui-form-item">
        <label class="layui-form-label">AppID</label>
        <div class="layui-input-block">
          <input type="text" id="${APP_ID_INPUT_ID}" class="layui-input" maxlength="64"
                 autocomplete="off" placeholder="QQ 开放平台给这台机器人的 AppID">
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">AppSecret</label>
        <div class="layui-input-block">
          <input type="password" id="${SECRET_INPUT_ID}" class="layui-input" maxlength="256"
                 autocomplete="new-password" placeholder="留空 = 不改（保存后任何接口都不回显）">
        </div>
        <div class="layui-form-mid layui-word-aux" style="padding:0 0 6px 110px">
          ⛔ 这是凭据：不要贴到聊天群 / 工单 / 截图里。保存后系统会拿它去官方换一次 token 验真。
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">已认证</label>
        <div class="layui-input-block">
          <input type="checkbox" id="${AUTH_SWITCH_ID}" name="${AUTH_SWITCH_NAME}"
                 lay-skin="switch" lay-text="已认证|未认证" lay-filter="${AUTH_SWITCH_FILTER}">
          <span class="layui-form-mid layui-word-aux">官方后台完成认证 ⇒ 限频 60 条/分钟（未认证 30）。</span>
        </div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">启用</label>
        <div class="layui-input-block">
          <input type="checkbox" id="${ENABLED_SWITCH_ID}" name="${ENABLED_SWITCH_NAME}"
                 lay-skin="switch" lay-text="启用|停用" lay-filter="${ENABLED_SWITCH_FILTER}">
          <span class="layui-form-mid layui-word-aux">修改凭据 / 认证状态后需重启 qqbot_ws 生效。</span>
        </div>
      </div>
    </div>
    <div style="text-align:center;padding:4px 0 10px">
      <button type="button" class="layui-btn layui-btn-normal" id="${BINDING_SAVE_BUTTON_ID}">保存密钥</button>
    </div>
    <div id="${BINDING_RESULT_ID}" style="line-height:24px"></div>
  </div>
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">连接状态与分享链接</div>
  <div class="layui-card-body">
    <div class="${EMPTY_HINT_CLASS}" style="color:#666;line-height:24px">${EMPTY_HINT_TEXT}</div>
    <div class="${CONFIGURED_ONLY_CLASS}" style="display:none">
      <div class="layui-form-item">
        <label class="layui-form-label">机器人名称</label>
        <div class="layui-input-block"><span class="layui-form-mid" id="${BOT_NAME_TEXT_ID}"></span></div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">连接状态</label>
        <div class="layui-input-block"><span class="layui-form-mid" id="${CONN_STATE_TEXT_ID}"></span></div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">最近心跳</label>
        <div class="layui-input-block"><span class="layui-form-mid" id="${LAST_ALIVE_TEXT_ID}"></span></div>
      </div>
      <div class="layui-form-item">
        <label class="layui-form-label">最近事件</label>
        <div class="layui-input-block"><span class="layui-form-mid" id="${LAST_EVENT_TEXT_ID}"></span></div>
      </div>
      <div id="${MARKDOWN_WARN_ID}" style="display:none;background:#FFB800;color:#fff;padding:8px 15px;margin:6px 0">
        Markdown 权限未生效，已自动改发纯文本。
      </div>
      <div class="layui-form-item" style="margin-top:10px">
        <label class="layui-form-label">分享链接</label>
        <div class="layui-input-block">
          <select id="${SHARE_GROUP_SELECT_ID}" style="width:220px"></select>
          <button type="button" class="layui-btn layui-btn-sm" id="${SHARE_BUTTON_ID}">生成分享链接</button>
          <span id="${SHARE_RESULT_ID}" style="margin-left:8px;word-break:break-all"></span>
          <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${SHARE_COPY_BUTTON_ID}"
                  style="display:none">复制链接</button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">群列表</div>
  <div class="layui-card-body">
    <div class="${EMPTY_HINT_CLASS}" style="color:#666;line-height:24px">${EMPTY_HINT_TEXT}</div>
    <div class="${CONFIGURED_ONLY_CLASS}" style="display:none">
      <div id="${GROUPS_WARN_ID}" style="color:#FF5722;line-height:24px"></div>
      <div id="${GROUPS_BODY_ID}"></div>
    </div>
  </div>
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">全局自定义菜单</div>
  <div class="layui-card-body">
    <div class="${EMPTY_HINT_CLASS}" style="color:#666;line-height:24px">${EMPTY_HINT_TEXT}</div>
    <div class="${CONFIGURED_ONLY_CLASS}" style="display:none">
      <div class="layui-form-item">
        <label class="layui-form-label">目标群</label>
        <div class="layui-input-block">
          <select id="${MENU_GROUP_SELECT_ID}" style="width:220px"></select>
          <button type="button" class="layui-btn layui-btn-sm" id="${MENU_LOAD_BUTTON_ID}">读取当前菜单</button>
          <span class="layui-form-mid layui-word-aux">当前版本：<span id="${MENU_VERSION_TEXT_ID}">—</span></span>
        </div>
      </div>
      <div id="${MENU_ROWS_ID}"></div>
      <div style="margin:8px 0">
        <button type="button" class="layui-btn layui-btn-sm" id="${MENU_ADD_BUTTON_ID}">添加一级菜单</button>
        <button type="button" class="layui-btn layui-btn-sm layui-btn-normal" id="${MENU_SAVE_BUTTON_ID}">保存到 QQ</button>
      </div>
      <div id="${MENU_MSG_ID}" style="line-height:24px"></div>
    </div>
  </div>
</div>

<div class="layui-card ${CARD_CLASS}">
  <div class="layui-card-header">推送记录</div>
  <div class="layui-card-body">
    <div class="${EMPTY_HINT_CLASS}" style="color:#666;line-height:24px">${EMPTY_HINT_TEXT}</div>
    <div class="${CONFIGURED_ONLY_CLASS}" style="display:none">
      <div style="margin-bottom:8px">
        <select id="${MSG_GROUP_FILTER_ID}" style="width:200px"></select>
        <select id="${MSG_STATUS_FILTER_ID}" style="width:140px">
          <option value="">全部状态</option>
          <option value="pending">pending</option>
          <option value="sent">sent</option>
          <option value="failed">failed</option>
        </select>
        <button type="button" class="layui-btn layui-btn-sm" id="${MSG_RELOAD_BUTTON_ID}">刷新</button>
      </div>
      <div id="${MSG_BODY_ID}"></div>
      <div style="margin-top:6px">
        <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${MSG_PREV_BUTTON_ID}">上一页</button>
        <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${MSG_NEXT_BUTTON_ID}">下一页</button>
        <span class="layui-form-mid layui-word-aux" id="${MSG_PAGE_INFO_ID}"></span>
      </div>
    </div>
  </div>
</div>`
}

export const __testables = {
  bindingSaveReport,
  buildMenuButtons,
  groupCardHtml,
  joinRequestsHtml,
  joinRowHtml,
  joinRowsHtml,
  memberNumText,
  menuLinkError,
  menuPayloadErrors,
  messageRowHtml,
  messageStatusColor,
  messagesHtml,
  mutesHtml,
  orWhitelistPending,
  parseMenuButtons,
  parseWhitelistInput,
  pushSummaryText,
  remoteErrorText,
  statusBadgeHtml,
  timeText,
}

export default {
  mount: async (root: HTMLElement, _ctx) => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式，再塞页面标记，最后 `initIslandElements`。
    const layui: LayuiNamespace = await bootstrapIsland(['layer', 'form'])
    const layer = layui.layer as LayuiModule
    // `form` 只为区块 1 的两颗 `lay-skin="switch"` 服务（见 `BINDING_FORM_FILTER` 的注释）。
    const form = (layui as unknown as { form?: LayuiModule }).form
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /** 重渲区块 1 的开关（`lay-skin="switch"` 的裸 input 必须经 `form.render` 才变成开关）。 */
    function renderBindingForm(): void {
      const render = (form as { render?: (type?: string, filter?: string) => void } | undefined)
        ?.render
      if (typeof render === 'function') render.call(form, 'checkbox', BINDING_FORM_FILTER)
    }

    // ⚠️ **本页自己的门禁**（与路由那两道门同源，⛔ 不是第二套判据；照 `pages/wecom-group-qr.ts`）：
    //    【第三方平台】组不能加组级 `accountAdminOnly`（会连累同组其它页），而这一页管的是
    //    机器人凭据与全群推送 ⇒ 页面内容收窄到账号管理员；iframe 可被深链打开，那一刻
    //    路由的 `requiresAccountAdmin` + `permissions` 都不生效。
    //    ⚠️ 注入标记必须排在门禁**之后**（被拒的深链连页面壳都不该看到）。
    const me = await fetchMe()
    if (me.is_account_admin !== true || !canNode(me, QQBOT_SETTINGS_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    // ── 页面状态 ────────────────────────────────────────────────────────────
    /** 服务端此刻的群清单（渲染与下拉的来源）。 */
    let groups: QQBotGroupRow[] = []
    /** 游戏字典（推送配置的多选；当前租户全量——配置面取 `fetchAllGames`，理由见 `api/config.ts`）。 */
    let games: ConfigRow[] = []
    /**
     * 每群已保存的白名单（编辑区的回填源）。**两个写入口**：
     * PATCH join-policy 的响应（写后读）＋ `loadGroups` 用群行的 `join_policy_whitelist`
     * 回填（刷新 / 重拉清单后名单不再丢）。
     */
    const savedWhitelists = new Map<number, string[]>()
    /** 菜单编辑器的状态（唯一事实来源；DOM 只是它的投影）。 */
    let menuRows: MenuTopRow[] = []
    let menuVersion: number | null = null
    /** 推送记录的页码与总数。 */
    let msgPage = 1
    let msgTotal = 0
    /** 分享链接的当前值（复制按钮从状态取，⛔ 不从 DOM 拼回来）。 */
    let shareUrl = ''

    /** 页面里某个节点（缺了就回 `null`，由调用方决定要不要早退）。 */
    function at<T extends HTMLElement>(id: string): T | null {
      return root.querySelector<T>(`#${id}`)
    }

    function setConfigured(on: boolean): void {
      for (const el of root.querySelectorAll<HTMLElement>(CONFIGURED_ONLY_SELECTOR)) {
        el.style.display = on ? '' : 'none'
      }
      for (const el of root.querySelectorAll<HTMLElement>(EMPTY_HINT_SELECTOR)) {
        el.style.display = on ? 'none' : ''
      }
    }

    /** 把凭据单例灌进页面（唯一的装配出口；`null` 不会出现——单例 `get_solo` 恒有一行）。 */
    function applyBinding(row: QQBotBinding): void {
      const appId = at<HTMLInputElement>(APP_ID_INPUT_ID)
      if (appId) appId.value = row.app_id
      const auth = at<HTMLInputElement>(AUTH_SWITCH_ID)
      if (auth) auth.checked = row.auth_verified
      const enabled = at<HTMLInputElement>(ENABLED_SWITCH_ID)
      if (enabled) enabled.checked = row.enabled
      renderBindingForm()
      const configured = row.app_id !== '' && row.app_secret_set
      setConfigured(configured)
      const botName = at(BOT_NAME_TEXT_ID)
      if (botName) botName.textContent = row.bot_name === '' ? '—' : row.bot_name
      const conn = at(CONN_STATE_TEXT_ID)
      if (conn) {
        conn.textContent = row.connection_state === '' ? '—' : row.connection_state
        conn.style.color = row.connection_state === 'online' ? '#009688' : '#666'
      }
      const alive = at(LAST_ALIVE_TEXT_ID)
      if (alive) alive.innerHTML = timeText(row.last_alive_at)
      const event = at(LAST_EVENT_TEXT_ID)
      if (event) event.innerHTML = timeText(row.last_event_at)
      const warn = at(MARKDOWN_WARN_ID)
      if (warn) warn.style.display = row.markdown_ok ? 'none' : ''
    }

    function setGroupsWarn(text: string): void {
      const warn = at(GROUPS_WARN_ID)
      if (warn) warn.textContent = text
    }

    /** 三个群下拉（分享 / 菜单 / 记录筛选）按当前群清单重填。 */
    function fillGroupSelects(): void {
      fillGroupSelect(at<HTMLSelectElement>(SHARE_GROUP_SELECT_ID), groups, '— 选一个群 —', false)
      fillGroupSelect(at<HTMLSelectElement>(MENU_GROUP_SELECT_ID), groups, '— 选一个群 —', false)
      fillGroupSelect(at<HTMLSelectElement>(MSG_GROUP_FILTER_ID), groups, '', true)
      const empty = groups.length === 0
      for (const id of [SHARE_BUTTON_ID, MENU_LOAD_BUTTON_ID, MENU_SAVE_BUTTON_ID]) {
        const button = at<HTMLButtonElement>(id)
        if (button) button.disabled = empty
      }
    }

    function renderGroups(): void {
      const body = at(GROUPS_BODY_ID)
      if (!body) return
      body.innerHTML =
        groups.length === 0
          ? `<div style="color:#666;line-height:24px">${EMPTY_GROUPS_TEXT}</div>`
          : groups.map((group) => groupCardHtml(group, games)).join('')
    }

    /**
     * 取群清单并上屏（可选 `refreshId` = 顺手让后端刷一个群的官方资料）。
     * ⚠️ 白名单未批**不是错误**：`info_error` 写进警示条、群行照出（空值由占位文案兜着）。
     */
    async function loadGroups(refreshId?: number): Promise<QQBotGroupsPayload> {
      if (games.length === 0) {
        try {
          games = await fetchAllGames()
        } catch (e) {
          setGroupsWarn(
            `游戏清单拉取失败：${e instanceof Error ? e.message : MESSAGES.failed}` +
              '（推送配置里的游戏勾选会缺项）',
          )
        }
      }
      const payload = await listQQBotGroups(refreshId)
      groups = payload.items
      // ⭐ 群行带回已存名单 ⇒ 就地回填（刷新后编辑区不再被清成空）。
      // 群行是服务端此刻的权威值，**覆盖**内存里那份（外部改过的名单也能跟上）。
      for (const group of groups) {
        savedWhitelists.set(group.id, group.join_policy_whitelist)
      }
      if (payload.info_error) {
        setGroupsWarn(`群资料刷新失败：${remoteErrorText(payload.info_error)}`)
      } else if (refreshId === undefined) {
        setGroupsWarn('')
      }
      fillGroupSelects()
      renderGroups()
      return payload
    }

    // ── 群卡片内的事件（卡片是动态渲染的 ⇒ 全部委托在容器上，绑定一次）─────────────

    function groupCardOf(target: EventTarget | null): HTMLElement | null {
      return (target as HTMLElement | null)?.closest?.(`.${GROUP_CARD_CLASS}`) ?? null
    }

    function groupIdOf(card: HTMLElement | null): number | null {
      const raw = card?.dataset.groupId
      if (raw === undefined || raw === '') return null
      const id = Number(raw)
      return Number.isInteger(id) ? id : null
    }

    /** 展开 / 收起一个折叠小块；返回它**这一次**是不是被展开了。 */
    function toggleArea(toggle: HTMLElement, body: HTMLElement, openText: string, closeText: string): boolean {
      const open = body.style.display === 'none'
      body.style.display = open ? '' : 'none'
      toggle.textContent = open ? closeText : openText
      return open
    }

    async function loadMutes(card: HTMLElement, groupId: number): Promise<void> {
      const body = card.querySelector<HTMLElement>(`.${MUTES_BODY_CLASS}`)
      if (!body) return
      try {
        body.innerHTML = mutesHtml(await listQQBotGroupMutes(groupId))
      } catch (e) {
        body.innerHTML = `<div style="color:#FF5722;line-height:24px">禁言列表拉取失败：${escapeHtml(
          e instanceof Error ? e.message : MESSAGES.failed,
        )}</div>`
      }
    }

    async function loadJoinRequests(card: HTMLElement, groupId: number): Promise<void> {
      const body = card.querySelector<HTMLElement>(`.${JR_BODY_CLASS}`)
      if (!body) return
      try {
        const payload = await listQQBotGroupJoinRequests(groupId)
        body.innerHTML = joinRequestsHtml(
          groups.find((group) => group.id === groupId),
          payload,
          savedWhitelists.get(groupId) ?? [],
        )
      } catch (e) {
        body.innerHTML = `<div style="color:#FF5722;line-height:24px">入群申请拉取失败：${escapeHtml(
          e instanceof Error ? e.message : MESSAGES.failed,
        )}</div>`
      }
    }

    at(GROUPS_BODY_ID)?.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null
      const card = groupCardOf(target)
      const groupId = groupIdOf(card)
      if (!target || !card || groupId === null) return
      if (target.closest(`.${REMARK_SAVE_CLASS}`) !== null) {
        const input = card.querySelector<HTMLInputElement>(`.${REMARK_INPUT_CLASS}`)
        if (!input) return
        void updateQQBotGroupRemark(groupId, input.value.trim())
          .then(() => loadGroups())
          .then(() => msg(MESSAGES.remarkSaved))
          .catch((e: Error) => msg(e.message))
        return
      }
      if (target.closest(`.${REFRESH_INFO_CLASS}`) !== null) {
        void loadGroups(groupId)
          .then((payload) => {
            msg(payload.info_error ? '群资料没刷新成（见群列表顶上的红字）' : MESSAGES.infoRefreshed)
          })
          .catch((e: Error) => msg(e.message))
        return
      }
      if (target.closest(`.${PUSH_SAVE_CLASS}`) !== null) {
        const active = card.querySelector<HTMLInputElement>(`.${PUSH_ACTIVE_CLASS}`)?.checked ?? false
        const typeInput = card.querySelector<HTMLInputElement>(`input[name="${PUSH_TYPE_PREFIX}-${groupId}"]:checked`)
        const gameIds = Array.from(card.querySelectorAll<HTMLInputElement>(`.${GAME_CHECK_CLASS}`))
          .filter((check) => check.checked)
          .map((check) => Number(check.dataset.gameId))
          .filter((id) => Number.isInteger(id))
        void putQQBotGroupPushConfig(groupId, {
          is_active: active,
          msg_type: typeInput?.value === 'text' ? 'text' : 'markdown',
          game_ids: gameIds,
        })
          .then(() => loadGroups())
          .then(() => msg(MESSAGES.pushSaved))
          .catch((e: Error) => msg(e.message))
        return
      }
      if (target.closest(`.${TEST_PUSH_CLASS}`) !== null) {
        void testQQBotGroupPush(groupId)
          .then((row) => msg(MESSAGES.testQueued(row.id, row.status)))
          .catch((e: Error) => msg(e.message))
        return
      }
      const groupToggle = target.closest(`.${GROUP_TOGGLE_CLASS}`)
      if (groupToggle !== null) {
        const detail = card.querySelector<HTMLElement>(`.${GROUP_DETAIL_CLASS}`)
        if (detail) {
          const open = detail.style.display === 'none'
          detail.style.display = open ? '' : 'none'
          ;(groupToggle as HTMLElement).textContent = open
            ? GROUP_TOGGLE_OPEN_TEXT
            : GROUP_TOGGLE_CLOSED_TEXT
        }
        return
      }
      const mutesToggle = target.closest(`.${MUTES_TOGGLE_CLASS}`)
      if (mutesToggle !== null) {
        const body = card.querySelector<HTMLElement>(`.${MUTES_BODY_CLASS}`)
        if (body) {
          const open = toggleArea(
            mutesToggle as HTMLElement,
            body,
            '禁言列表（点这里展开拉取）',
            '禁言列表（点这里收起）',
          )
          if (open) void loadMutes(card, groupId)
        }
        return
      }
      const jrToggle = target.closest(`.${JR_TOGGLE_CLASS}`)
      if (jrToggle !== null) {
        const body = card.querySelector<HTMLElement>(`.${JR_BODY_CLASS}`)
        if (body) {
          const open = toggleArea(
            jrToggle as HTMLElement,
            body,
            '入群申请与审批策略（点这里展开拉取）',
            '入群申请与审批策略（点这里收起）',
          )
          if (open) void loadJoinRequests(card, groupId)
        }
        return
      }
      if (target.closest(`.${POLICY_SAVE_CLASS}`) !== null) {
        const select = card.querySelector<HTMLSelectElement>(`.${POLICY_SELECT_CLASS}`)
        const input = card.querySelector<HTMLTextAreaElement>(`.${WHITELIST_INPUT_CLASS}`)
        if (!select) return
        // ⚠️ select.value 是裸串：不在取值表里（被改坏的下拉）就按 `off` 走，⛔ 不硬塞给后端。
        const mode = (
          POLICY_MODE_OPTIONS.some(([value]) => value === select.value) ? select.value : 'off'
        ) as QQBotJoinPolicyMode
        const whitelist = mode === 'whitelist' && input ? parseWhitelistInput(input.value) : []
        void updateQQBotGroupJoinPolicy(groupId, { mode, whitelist })
          .then((saved) => {
            savedWhitelists.set(groupId, saved.join_policy_whitelist)
            return loadJoinRequests(card, groupId)
          })
          .then(() => msg(MESSAGES.policySaved))
          .catch((e: Error) => msg(e.message))
        return
      }
      const approveButton = target.closest(`.${JR_APPROVE_CLASS}`)
      const declineButton = target.closest(`.${JR_DECLINE_CLASS}`)
      if (approveButton !== null || declineButton !== null) {
        const button = (approveButton ?? declineButton) as HTMLElement
        const joinRequestId = decodeURIComponent(button.dataset.jrId ?? '')
        const memberOpenid = decodeURIComponent(button.dataset.jrOpenid ?? '')
        if (joinRequestId === '' || memberOpenid === '') return
        const op = approveButton !== null ? 'approve' : 'decline'
        void approveQQBotJoinRequest(groupId, {
          op,
          join_request_id: joinRequestId,
          member_openid: memberOpenid,
        })
          .then((result) => {
            if (!result.ok) {
              msg(`操作失败：${result.error ? remoteErrorText(result.error) : '远端没有给出原因'}`)
            } else {
              msg(op === 'approve' ? MESSAGES.joinApproved : MESSAGES.joinDeclined)
            }
            return loadJoinRequests(card, groupId)
          })
          .catch((e: Error) => msg(e.message))
      }
    })

    // 策略下拉切到 whitelist 才放开名单编辑（其余模式后端会把名单清成 []，编辑是误导）。
    at(GROUPS_BODY_ID)?.addEventListener('change', (event) => {
      const target = event.target as HTMLElement | null
      if (!target || target.closest(`.${POLICY_SELECT_CLASS}`) === null) return
      const card = groupCardOf(target)
      const select = target as HTMLSelectElement
      const input = card?.querySelector<HTMLTextAreaElement>(`.${WHITELIST_INPUT_CLASS}`)
      if (input) input.disabled = select.value !== 'whitelist'
    })

    // ── 区块 1：保存密钥 ────────────────────────────────────────────────────

    at<HTMLButtonElement>(BINDING_SAVE_BUTTON_ID)?.addEventListener('click', () => {
      const result = at(BINDING_RESULT_ID)
      const appId = at<HTMLInputElement>(APP_ID_INPUT_ID)?.value.trim() ?? ''
      const secret = at<HTMLInputElement>(SECRET_INPUT_ID)?.value ?? ''
      const payload: {
        app_id: string
        enabled: boolean
        auth_verified: boolean
        app_secret?: string
      } = {
        app_id: appId,
        enabled: at<HTMLInputElement>(ENABLED_SWITCH_ID)?.checked ?? false,
        auth_verified: at<HTMLInputElement>(AUTH_SWITCH_ID)?.checked ?? false,
      }
      // ⚠️ **留空 = 不改**：secret 为空就**不发这个键**（后端把空串也当「不动它」，但
      //    「键在不在」才是页面侧可读的判据；真的想换 secret 就填新值）。
      if (secret !== '') payload.app_secret = secret
      void updateQQBotBinding(payload)
        .then((saved) => {
          applyBinding(saved)
          if (result) result.innerHTML = bindingSaveReport(saved)
          // 从「没配置」变「配置好了」这一刻，把群 / 记录拉起来（空态引导刚被藏掉）。
          if (saved.app_id !== '' && saved.app_secret_set) {
            void loadGroups().catch((e: Error) => msg(e.message))
            void loadMessages().catch((e: Error) => setMsgBodyError(e.message))
          }
        })
        .catch((e: Error) => {
          if (result) {
            result.innerHTML = `<span style="color:#FF5722">${escapeHtml(e.message)}</span>`
          }
        })
    })

    // ── 区块 2：分享链接 ────────────────────────────────────────────────────

    at<HTMLButtonElement>(SHARE_BUTTON_ID)?.addEventListener('click', () => {
      const raw = at<HTMLSelectElement>(SHARE_GROUP_SELECT_ID)?.value ?? ''
      const groupId = Number(raw)
      if (!Number.isInteger(groupId) || groupId <= 0) {
        msg(MESSAGES.menuPickGroup)
        return
      }
      const result = at(SHARE_RESULT_ID)
      const copyButton = at<HTMLButtonElement>(SHARE_COPY_BUTTON_ID)
      void createQQBotShareLink(groupId)
        .then((payload) => {
          if (payload.url !== null && payload.url !== '') {
            shareUrl = payload.url
            if (result) result.innerHTML = `<span style="color:#009688">${escapeHtml(payload.url)}</span>`
            if (copyButton) copyButton.style.display = ''
          } else {
            shareUrl = ''
            const reason = payload.remote_error
              ? remoteErrorText(payload.remote_error)
              : '远端没有返回链接'
            if (result) result.innerHTML = `<span style="color:#FF5722">生成失败：${escapeHtml(reason)}</span>`
            if (copyButton) copyButton.style.display = 'none'
          }
        })
        .catch((e: Error) => {
          shareUrl = ''
          if (result) result.innerHTML = `<span style="color:#FF5722">${escapeHtml(e.message)}</span>`
          if (copyButton) copyButton.style.display = 'none'
        })
    })

    at<HTMLButtonElement>(SHARE_COPY_BUTTON_ID)?.addEventListener('click', () => {
      copyTextToClipboard(shareUrl, (ok) =>
        msg(ok ? '已复制分享链接' : MESSAGES.copyFailed),
      )
    })

    // ── 区块 4：菜单编辑器（状态 → 渲染；输入只改状态，结构变化才重画）────────────

    function renderMenuRows(): void {
      const body = at(MENU_ROWS_ID)
      if (!body) return
      if (menuRows.length === 0) {
        body.innerHTML =
          '<div style="color:#666;line-height:24px">还没有菜单行：点「添加一级菜单」开始，或先「读取当前菜单」。</div>'
        return
      }
      body.innerHTML = menuRows        .map((row, i) => {
          const topOptions = MENU_TOP_TYPES.map(
            (value) => `<option value="${value}"${value === row.type ? ' selected' : ''}>${value}</option>`,
          ).join('')
          const subs = row.subs
            .map((sub, j) => {
              const subSelected = (value: string): string => (sub.type === value ? ' selected' : '')
              return (
                `<div class="${MENU_SUB_ROW_CLASS}" data-sub="${j}" style="margin:4px 0 0 24px">` +
                `├ <input type="text" class="layui-input ${MENU_SUB_NAME_CLASS}" style="display:inline-block;width:140px" ` +
                `value="${escapeHtml(sub.name)}" placeholder="子菜单名（≤14 字）"> ` +
                `<select class="${MENU_SUB_TYPE_CLASS}">${MENU_SUB_TYPES.map(
                  (value) => `<option value="${value}"${subSelected(value)}>${value}</option>`,
                ).join('')}</select> ` +
                `<input type="text" class="layui-input ${MENU_SUB_VALUE_CLASS}" style="display:inline-block;width:220px" ` +
                `value="${escapeHtml(sub.link)}" placeholder="https://…（link 用）"${sub.type === 'link' ? '' : ' disabled'}> ` +
                `<button type="button" class="layui-btn layui-btn-xs layui-btn-danger ${MENU_SUB_DEL_CLASS}">删</button>` +
                '</div>'
              )
            })
            .join('')
          return (
            `<div class="${MENU_ROW_CLASS}" data-row="${i}" style="border-bottom:1px dashed #eee;padding:6px 0">` +
            `<input type="text" class="layui-input ${MENU_ROW_NAME_CLASS}" style="display:inline-block;width:160px" ` +
            `value="${escapeHtml(row.name)}" placeholder="菜单名（≤10 字）"> ` +
            `<select class="${MENU_ROW_TYPE_CLASS}">${topOptions}</select> ` +
            `<input type="text" class="layui-input ${MENU_ROW_VALUE_CLASS}" style="display:inline-block;width:240px" ` +
            `value="${escapeHtml(row.link)}" placeholder="https://…（link 类型用）"${row.type === 'link' && row.subs.length === 0 ? '' : ' disabled'}> ` +
            `<button type="button" class="layui-btn layui-btn-xs ${MENU_ROW_ADDSUB_CLASS}">+子菜单</button> ` +
            `<button type="button" class="layui-btn layui-btn-xs layui-btn-danger ${MENU_ROW_DEL_CLASS}">删</button>` +
            subs +
            '</div>'
          )
        })
        .join('')
    }

    function menuMsg(text: string, bad: boolean): void {
      const box = at(MENU_MSG_ID)
      if (box) box.innerHTML = `<span style="color:${bad ? '#FF5722' : '#009688'}">${escapeHtml(text)}</span>`
    }

    at<HTMLButtonElement>(MENU_ADD_BUTTON_ID)?.addEventListener('click', () => {
      menuRows.push(emptyMenuRow())
      renderMenuRows()
    })

    at(MENU_ROWS_ID)?.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null
      if (!target) return
      const rowEl = target.closest(`.${MENU_ROW_CLASS}`) as HTMLElement | null
      if (rowEl === null) return
      const rowIndex = Number(rowEl.dataset.row)
      const row = menuRows[rowIndex]
      if (!row) return
      if (target.closest(`.${MENU_ROW_DEL_CLASS}`) !== null) {
        menuRows.splice(rowIndex, 1)
        renderMenuRows()
        return
      }
      if (target.closest(`.${MENU_ROW_ADDSUB_CLASS}`) !== null) {
        row.subs.push(emptyMenuSubRow())
        renderMenuRows()
        return
      }
      const subEl = target.closest(`.${MENU_SUB_ROW_CLASS}`) as HTMLElement | null
      if (subEl !== null && target.closest(`.${MENU_SUB_DEL_CLASS}`) !== null) {
        const subIndex = Number(subEl.dataset.sub)
        if (row.subs[subIndex] !== undefined) {
          row.subs.splice(subIndex, 1)
          renderMenuRows()
        }
      }
    })

    at(MENU_ROWS_ID)?.addEventListener('input', (event) => {
      const target = event.target as HTMLInputElement | null
      if (!target) return
      const rowEl = target.closest(`.${MENU_ROW_CLASS}`) as HTMLElement | null
      if (rowEl === null) return
      const row = menuRows[Number(rowEl.dataset.row)]
      if (!row) return
      if (target.closest(`.${MENU_ROW_NAME_CLASS}`) !== null) row.name = target.value
      else if (target.closest(`.${MENU_ROW_VALUE_CLASS}`) !== null) row.link = target.value
      else {
        const subEl = target.closest(`.${MENU_SUB_ROW_CLASS}`) as HTMLElement | null
        const sub = subEl === null ? undefined : row.subs[Number(subEl.dataset.sub)]
        if (!sub) return
        if (target.closest(`.${MENU_SUB_NAME_CLASS}`) !== null) sub.name = target.value
        else if (target.closest(`.${MENU_SUB_VALUE_CLASS}`) !== null) sub.link = target.value
      }
    })

    at(MENU_ROWS_ID)?.addEventListener('change', (event) => {
      const target = event.target as HTMLSelectElement | null
      if (!target) return
      const rowEl = target.closest(`.${MENU_ROW_CLASS}`) as HTMLElement | null
      if (rowEl === null) return
      const row = menuRows[Number(rowEl.dataset.row)]
      if (!row) return
      if (target.closest(`.${MENU_ROW_TYPE_CLASS}`) !== null) {
        row.type = (MENU_TOP_TYPES as readonly string[]).includes(target.value)
          ? (target.value as MenuTopType)
          : 'switch'
        renderMenuRows()
        return
      }
      const subEl = target.closest(`.${MENU_SUB_ROW_CLASS}`) as HTMLElement | null
      const sub = subEl === null ? undefined : row.subs[Number(subEl.dataset.sub)]
      if (!sub) return
      if (target.closest(`.${MENU_SUB_TYPE_CLASS}`) !== null) {
        sub.type = (MENU_SUB_TYPES as readonly string[]).includes(target.value)
          ? (target.value as MenuSubType)
          : 'send_message'
        renderMenuRows()
      }
    })

    at<HTMLButtonElement>(MENU_LOAD_BUTTON_ID)?.addEventListener('click', () => {
      const raw = at<HTMLSelectElement>(MENU_GROUP_SELECT_ID)?.value ?? ''
      const groupId = Number(raw)
      if (!Number.isInteger(groupId) || groupId <= 0) {
        menuMsg(MESSAGES.menuPickGroup, true)
        return
      }
      void getQQBotMenu(groupId)
        .then((payload) => {
          if (payload.remote_error) {
            menuMsg(`读取菜单失败：${remoteErrorText(payload.remote_error)}`, true)
            return
          }
          const parsed = parseMenuButtons(payload.menu)
          if (parsed === null) {
            menuMsg('读取菜单失败：官方回包不是可编辑的菜单形状（原样已打到控制台日志面之外——保存会被拒）。', true)
            return
          }
          menuRows = parsed
          menuVersion = payload.version
          const version = at(MENU_VERSION_TEXT_ID)
          if (version) version.textContent = payload.version === null ? '—' : String(payload.version)
          renderMenuRows()
          menuMsg(`已读取当前菜单（${menuRows.length} 个一级）。`, false)
        })
        .catch((e: Error) => menuMsg(e.message, true))
    })

    at<HTMLButtonElement>(MENU_SAVE_BUTTON_ID)?.addEventListener('click', () => {
      const raw = at<HTMLSelectElement>(MENU_GROUP_SELECT_ID)?.value ?? ''
      const groupId = Number(raw)
      if (!Number.isInteger(groupId) || groupId <= 0) {
        menuMsg(MESSAGES.menuPickGroup, true)
        return
      }
      const errors = menuPayloadErrors(menuRows)
      if (errors.length > 0) {
        menuMsg(errors.join('；'), true)
        return
      }
      void putQQBotMenu({ group: groupId, menu: { buttons: buildMenuButtons(menuRows) } })
        .then((payload) => {
          if (!payload.ok) {
            const reason = payload.remote_error
              ? remoteErrorText(payload.remote_error)
              : '远端没有给出原因'
            menuMsg(`保存失败：${reason}`, true)
            return
          }
          menuVersion = payload.version ?? null
          const version = at(MENU_VERSION_TEXT_ID)
          if (version) version.textContent = menuVersion === null ? '—' : String(menuVersion)
          menuMsg(MESSAGES.menuSaved(menuVersion), false)
        })
        .catch((e: Error) => menuMsg(e.message, true))
    })

    // ── 区块 5：推送记录 ────────────────────────────────────────────────────

    function setMsgBodyError(text: string): void {
      const body = at(MSG_BODY_ID)
      if (body) body.innerHTML = `<div style="color:#FF5722;line-height:24px">${escapeHtml(text)}</div>`
    }

    async function loadMessages(): Promise<void> {
      const groupRaw = at<HTMLSelectElement>(MSG_GROUP_FILTER_ID)?.value ?? ''
      const statusRaw = at<HTMLSelectElement>(MSG_STATUS_FILTER_ID)?.value ?? ''
      const page = await listQQBotMessages({
        group: groupRaw === '' ? undefined : Number(groupRaw),
        status: statusRaw === '' ? undefined : (statusRaw as 'pending' | 'sent' | 'failed'),
        page: msgPage,
      })
      msgTotal = page.total
      const body = at(MSG_BODY_ID)
      if (body) body.innerHTML = messagesHtml(page.items)
      const info = at(MSG_PAGE_INFO_ID)
      if (info) info.textContent = `第 ${page.page} 页 · 共 ${page.total} 条`
      const prev = at<HTMLButtonElement>(MSG_PREV_BUTTON_ID)
      if (prev) prev.disabled = page.page <= 1
      const next = at<HTMLButtonElement>(MSG_NEXT_BUTTON_ID)
      if (next) next.disabled = page.page * page.page_size >= page.total
    }

    at<HTMLButtonElement>(MSG_RELOAD_BUTTON_ID)?.addEventListener('click', () => {
      msgPage = 1
      void loadMessages().catch((e: Error) => setMsgBodyError(e.message))
    })
    at<HTMLSelectElement>(MSG_GROUP_FILTER_ID)?.addEventListener('change', () => {
      msgPage = 1
      void loadMessages().catch((e: Error) => setMsgBodyError(e.message))
    })
    at<HTMLSelectElement>(MSG_STATUS_FILTER_ID)?.addEventListener('change', () => {
      msgPage = 1
      void loadMessages().catch((e: Error) => setMsgBodyError(e.message))
    })
    at<HTMLButtonElement>(MSG_PREV_BUTTON_ID)?.addEventListener('click', () => {
      if (msgPage > 1) msgPage -= 1
      void loadMessages().catch((e: Error) => setMsgBodyError(e.message))
    })
    at<HTMLButtonElement>(MSG_NEXT_BUTTON_ID)?.addEventListener('click', () => {
      if (msgPage * MESSAGE_PAGE_SIZE < msgTotal) msgPage += 1
      void loadMessages().catch((e: Error) => setMsgBodyError(e.message))
    })

    // ── 初装：先凭据，配置好了再拉群与记录 ────────────────────────────────────
    try {
      const row = await getQQBotBinding()
      applyBinding(row)
      if (row.app_id !== '' && row.app_secret_set) {
        await loadGroups()
        await loadMessages().catch((e: Error) => setMsgBodyError(e.message))
      }
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
