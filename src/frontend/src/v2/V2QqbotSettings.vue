<script setup lang="ts">
/**
 * 「QQ机器人设置」v2（`/v2/qqbot-settings`；2026-11-06 第三方平台批 B4，计划 §5）。
 * 事实源 = island 自有功能页 `layui-island/pages/qqbot-settings.ts`（文案与行为逐字照抄）；
 * 端点契约 = `api/qqbot.ts`。⛔ 不 import island 页模块；色一律走 carbon token。
 * 页内权限门 fail-closed：账号管理员 + 节点 `system.qqbot_settings` 双判（同旧页深链自检）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { fetchAllGames, type ConfigRow } from '@/api/config'
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
  type QQBotBindingPatch,
  type QQBotBindingSaveResult,
  type QQBotGroupRow,
  type QQBotGroupStatus,
  type QQBotGroupsPayload,
  type QQBotJoinPolicyMode,
  type QQBotJoinRequestRow,
  type QQBotJoinRequestsPayload,
  type QQBotMutesPayload,
  type QQBotMessageRow,
  type QQBotMessageStatus,
  type QQBotMsgType,
  type QQBotPushConfigSummary,
  type QQBotRemoteError,
} from '@/api/qqbot'
import { useSessionStore } from '@/stores/session'

/* ── 逐字文案（出处 = island 页常量）────────────────────────────────────────── */

const EMPTY_HINT_TEXT = '先在上方保存密钥（AppID 与 AppSecret），保存通过后这里才能取数。'
const NO_ACCESS_MESSAGE = '无权限管理 QQ 机器人设置'
const WHITELIST_PENDING_TEXT = '白名单未批'
const EMPTY_GROUPS_TEXT = '还没有群：机器人被拉进某个群、收到第一条消息后，群会出现在这里。'
const MARKDOWN_WARN_TEXT = 'Markdown 权限未生效，已自动改发纯文本。'
const APP_ID_PLACEHOLDER = 'QQ 开放平台给这台机器人的 AppID'
const SECRET_PLACEHOLDER = '留空表示不修改'
const REMARK_PLACEHOLDER = '本地备注（只自己看得见）'
const GROUP_TOGGLE_CLOSED_TEXT = '群设置（推送 / 测试 / 禁言 / 入群申请）— 点这里展开'
const GROUP_TOGGLE_OPEN_TEXT = '群设置（推送 / 测试 / 禁言 / 入群申请）— 点这里收起'
const MUTES_CLOSED_TEXT = '禁言列表（点这里展开拉取）'
const MUTES_OPEN_TEXT = '禁言列表（点这里收起）'
const JR_CLOSED_TEXT = '入群申请与审批策略（点这里展开拉取）'
const JR_OPEN_TEXT = '入群申请与审批策略（点这里收起）'
const MUTES_EMPTY_TEXT = '当前没有禁言中的成员。'
const MUTES_FAIL_PREFIX = '禁言列表拉取失败：'
const JR_EMPTY_TEXT = '本地还没有入群申请的留痕。'
const JR_FAIL_PREFIX = '入群申请拉取失败：'
const JR_REMOTE_FAIL_PREFIX = '远端拉取失败：'
const REMOTE_NONE_TEXT = '远端没有返回原因'
const REMOTE_BLANK_TEXT = '远端这一块没有返回。'
const REMOTE_RAW_PREFIX = '远端返回（原样）：'
const NO_REMOTE_REASON_TEXT = '远端没有给出原因'
const SHARE_PLACEHOLDER = '— 选一个群 —'
const SHARE_FAIL_PREFIX = '生成失败：'
const SHARE_NO_URL_TEXT = '远端没有返回链接'
const SHARE_COPIED_TEXT = '已复制分享链接'
const NO_GAME_TEXT = '（当前租户没有游戏可选）'
const MENU_EMPTY_TEXT = '还没有菜单行：点「添加一级菜单」开始，或先「读取当前菜单」。'
const MENU_PARSE_FAIL_TEXT =
  '读取菜单失败：官方回包不是可编辑的菜单形状（原样已打到控制台日志面之外——保存会被拒）。'
const MENU_NAME_PLACEHOLDER = '菜单名（≤10 字）'
const MENU_SUB_NAME_PLACEHOLDER = '子菜单名（≤14 字）'
const MENU_LINK_PLACEHOLDER = 'https://…（link 类型用）'
const MENU_SUB_LINK_PLACEHOLDER = 'https://…（link 用）'
const MSG_EMPTY_TEXT = '还没有推送记录。'

const MESSAGES = {
  failed: '操作失败',
  copyFailed: '复制失败（浏览器不允许自动复制），请手动选中链接复制',
  remarkSaved: '备注已保存',
  infoRefreshed: '群资料已刷新',
  pushSaved: '推送配置已保存',
  policySaved: '审批策略已保存',
  joinApproved: '已通过',
  joinDeclined: '已拒绝',
  menuPickGroup: '先选一个群',
  refreshStale: '群资料没刷新成（见群列表顶上的红字）',
  gamesFailPrefix: '游戏清单拉取失败：',
  gamesFailSuffix: '（推送配置里的游戏勾选会缺项）',
  groupsWarnPrefix: '群资料刷新失败：',
} as const

function testQueuedText(id: number, status: string): string {
  return `测试消息已入队（发件箱记录 #${id}，当前状态 ${status}；发没发出去看下面的推送记录）`
}

function menuSavedText(version: number | null): string {
  return version === null ? '菜单已保存（官方没回版本号）' : `菜单已保存，版本 ${version}`
}

/** 审批策略下拉的取值与中文（取值 = 后端 JoinPolicyMode 逐字；中文照模型 label）。 */
const POLICY_MODE_OPTIONS: ReadonlyArray<readonly [string, string]> = [
  ['off', '不自动处理（留人工）'],
  ['approve_all', '全部自动通过'],
  ['decline_all', '全部自动拒绝'],
  ['whitelist', '白名单自动通过，其余拒绝'],
]

/* ── 纯函数（island 页逐字移植；Vue 插值自带转义，escapeHtml 不再需要）────────── */

function errText(e: unknown): string {
  return e instanceof Error ? e.message : MESSAGES.failed
}

/** 为空显示占位（判据是空串——`0` 之类的值不会被它吃掉）。 */
function orWhitelistPending(value: string): string {
  return value === '' ? WHITELIST_PENDING_TEXT : value
}

/** 成员数：`null` = 资料没拿到 ⇒ 占位（0 是合法边界，E-0021）。 */
function memberNumText(value: number | null): string {
  return value === null ? WHITELIST_PENDING_TEXT : String(value)
}

/** ISO 8601 时刻的显示形态（截到秒、不做时区换算）。 */
function timeText(value: string | null): string {
  if (value === null || value === '') return '—'
  return value.replace('T', ' ').slice(0, 19)
}

/** 推送配置摘要（`null` = 还没配过）。 */
function pushSummaryText(config: QQBotPushConfigSummary | null): string {
  if (config === null) return '推送：未配置'
  const on = config.is_active ? '已启用' : '已停用'
  return `推送${on} · ${config.msg_type} · 限定游戏 ${config.game_ids.length} 个`
}

/** 远端 / 白名单降级错误的一句话（code 是官方字符串，原样带上）。 */
function remoteErrorText(err: QQBotRemoteError): string {
  return `${err.message}（code ${err.code}）`
}

/** 群在某处的显示名（备注优先，其次群名，最后 openid）。 */
function groupDisplayName(group: QQBotGroupRow): string {
  return group.remark || group.group_name || group.group_openid
}

/** 白名单输入的解析：换行 / 半角与全角逗号分隔、去空白、去重（保序）。 */
function parseWhitelistInput(text: string): string[] {
  const out: string[] = []
  for (const part of text.split(/[\n,，]/)) {
    const value = part.trim()
    if (value !== '' && !out.includes(value)) out.push(value)
  }
  return out
}

/* ── 菜单编辑器的纯函数（上限 / 类型白名单 / https——后端仍是最终裁决者）───────── */

const MENU_TOP_TYPES = ['switch', 'send_message', 'link', 'menu'] as const
const MENU_SUB_TYPES = ['send_message', 'link'] as const
type MenuTopType = (typeof MENU_TOP_TYPES)[number]
type MenuSubType = (typeof MENU_SUB_TYPES)[number]
type MenuSubRow = { name: string; type: MenuSubType; link: string }
type MenuTopRow = { name: string; type: MenuTopType; link: string; subs: MenuSubRow[] }
/** 官方菜单行数上限（后端校验同值；前端先拦一次）。 */
const MENU_TOP_LIMIT = 10
const MENU_SUB_LIMIT = 5

function emptyMenuRow(): MenuTopRow {
  return { name: '', type: 'switch', link: '', subs: [] }
}

function emptyMenuSubRow(): MenuSubRow {
  return { name: '', type: 'send_message', link: '' }
}

/** https 的前端即时校验（与后端 startsWith('https://') 同一条判据）。 */
function menuLinkError(link: string): string | null {
  const trimmed = link.trim()
  if (trimmed === '' || !trimmed.startsWith('https://')) {
    return 'link 类型菜单的链接必须以 https:// 开头'
  }
  return null
}

/** 保存前的全部问题（逐条中文；空数组 = 可以保存）。 */
function menuPayloadErrors(rows: readonly MenuTopRow[]): string[] {
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

/** 编辑器状态 → 官方载荷（`{name, type?, link?, sub_button?}`）。 */
function buildMenuButtons(rows: readonly MenuTopRow[]): unknown[] {
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

/** 官方菜单回包 → 编辑器状态；只认 `{buttons: [...]}`，其它形状回 null。 */
function parseMenuButtons(menu: unknown): MenuTopRow[] | null {
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

function jsonText(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

/** 「复制」的剪贴板出口（缺 navigator.clipboard 不是静默失败）。 */
function copyTextToClipboard(text: string, onDone: (ok: boolean) => void): void {
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

/** 保存密钥之后那句回报（verified 只在带了凭据格时出现——三条分支对着真实后果说）。 */
function bindingSaveReport(saved: QQBotBindingSaveResult): { bad: boolean; text: string } {
  if (saved.verified === true) {
    return { bad: false, text: '已保存：凭据验真通过（官方 token 换取成功）。' }
  }
  if (saved.verified === false) {
    const reason =
      saved.last_error === '' ? '（后端没有给出原因）' : saved.last_error
    return {
      bad: true,
      text: `已保存，但验真失败——连接不会建立。最近错误：${reason}。请核对 AppID / AppSecret 后重存。`,
    }
  }
  return { bad: false, text: '已保存（本次未进行凭据验真）。' }
}

/* ── 权限门 / toast ──────────────────────────────────────────────────────────── */

const session = useSessionStore()
const canView = computed(
  () => session.isAccountAdmin === true && session.canNode([QQBOT_SETTINGS_NODE]),
)

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 页面状态 ───────────────────────────────────────────────────────────────── */

const binding = ref<QQBotBinding | null>(null)
const configured = computed(
  () => binding.value !== null && binding.value.app_id !== '' && binding.value.app_secret_set,
)

type PushDraft = { is_active: boolean; msg_type: QQBotMsgType; game_ids: number[] }
type MuteView = { open: boolean; payload: QQBotMutesPayload | null; error: string | null }
type JoinView = {
  open: boolean
  payload: QQBotJoinRequestsPayload | null
  error: string | null
  mode: QQBotJoinPolicyMode
  whitelistText: string
}
type GroupView = {
  row: QQBotGroupRow
  remark: string
  push: PushDraft
  expanded: boolean
  mutes: MuteView | null
  join: JoinView | null
}

const groups = ref<GroupView[]>([])
const games = ref<ConfigRow[]>([])
const groupsWarn = ref('')

/* ── 区块 1：机器人密钥 ─────────────────────────────────────────────────────── */

const form = reactive({ appId: '', secret: '', authVerified: false, enabled: false })
const bindingResult = ref<{ bad: boolean; text: string } | null>(null)

function applyBinding(row: QQBotBinding): void {
  binding.value = row
  form.appId = row.app_id
  form.authVerified = row.auth_verified
  form.enabled = row.enabled
}

async function saveBinding(): Promise<void> {
  const payload: QQBotBindingPatch = {
    app_id: form.appId.trim(),
    enabled: form.enabled,
    auth_verified: form.authVerified,
  }
  // 留空 = 不改：secret 为空就不发这个键（⛔ 不发空串当「不改」）。
  if (form.secret !== '') payload.app_secret = form.secret
  const needsRestart = form.secret !== '' || binding.value === null ||
    payload.app_id !== binding.value.app_id || payload.auth_verified !== binding.value.auth_verified
  try {
    const saved = await updateQQBotBinding(payload)
    applyBinding(saved)
    const result = bindingSaveReport(saved)
    bindingResult.value = needsRestart
      ? { ...result, text: `${result.text} 修改密钥或认证状态后，请重启 QQ 机器人服务使配置生效。` }
      : result
    if (saved.app_id !== '' && saved.app_secret_set) {
      void loadGroups().catch((e: unknown) => showToast(errText(e), 'error'))
      void loadMessages()
    }
  } catch (e) {
    bindingResult.value = { bad: true, text: errText(e) }
  }
}

/* ── 区块 2：连接状态与分享链接 ─────────────────────────────────────────────── */

const shareGroupId = ref('')
const shareUrl = ref('')
const shareResult = ref<{ ok: boolean; text: string } | null>(null)

async function createShare(): Promise<void> {
  const groupId = Number(shareGroupId.value)
  if (!Number.isInteger(groupId) || groupId <= 0) {
    showToast(MESSAGES.menuPickGroup)
    return
  }
  try {
    const payload = await createQQBotShareLink(groupId)
    if (payload.url !== null && payload.url !== '') {
      shareUrl.value = payload.url
      shareResult.value = { ok: true, text: payload.url }
    } else {
      shareUrl.value = ''
      const reason = payload.remote_error
        ? remoteErrorText(payload.remote_error)
        : SHARE_NO_URL_TEXT
      shareResult.value = { ok: false, text: `${SHARE_FAIL_PREFIX}${reason}` }
    }
  } catch (e) {
    shareUrl.value = ''
    shareResult.value = { ok: false, text: errText(e) }
  }
}

function copyShare(): void {
  copyTextToClipboard(shareUrl.value, (ok) => showToast(ok ? SHARE_COPIED_TEXT : MESSAGES.copyFailed))
}

/* ── 区块 3：群列表 ─────────────────────────────────────────────────────────── */

/** 群清单重拉 = 卡片整体重渲（照旧页 renderGroups：展开区收起、草稿按已存值重置）。 */
async function loadGroups(refreshId?: number): Promise<QQBotGroupsPayload> {
  if (games.value.length === 0) {
    try {
      games.value = await fetchAllGames()
    } catch (e) {
      groupsWarn.value = `${MESSAGES.gamesFailPrefix}${errText(e)}${MESSAGES.gamesFailSuffix}`
    }
  }
  const payload = await listQQBotGroups(refreshId)
  groups.value = payload.items.map((row) => ({
    row,
    remark: row.remark,
    push:
      row.push_config === null
        ? { is_active: true, msg_type: 'markdown' as QQBotMsgType, game_ids: [] }
        : {
            is_active: row.push_config.is_active,
            msg_type: row.push_config.msg_type,
            game_ids: [...row.push_config.game_ids],
          },
    expanded: false,
    mutes: null,
    join: null,
  }))
  if (payload.info_error) {
    groupsWarn.value = `${MESSAGES.groupsWarnPrefix}${remoteErrorText(payload.info_error)}`
  } else if (refreshId === undefined) {
    groupsWarn.value = ''
  }
  return payload
}

async function saveRemark(view: GroupView): Promise<void> {
  try {
    await updateQQBotGroupRemark(view.row.id, view.remark.trim())
    await loadGroups()
    showToast(MESSAGES.remarkSaved)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

async function refreshInfo(gid: number): Promise<void> {
  try {
    const payload = await loadGroups(gid)
    showToast(payload.info_error ? MESSAGES.refreshStale : MESSAGES.infoRefreshed)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

async function savePush(view: GroupView): Promise<void> {
  const gameIds = view.push.game_ids.filter((id) => Number.isInteger(id))
  try {
    await putQQBotGroupPushConfig(view.row.id, {
      is_active: view.push.is_active,
      msg_type: view.push.msg_type,
      game_ids: gameIds,
    })
    await loadGroups()
    showToast(MESSAGES.pushSaved)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

async function testPush(gid: number): Promise<void> {
  try {
    const row = await testQQBotGroupPush(gid)
    showToast(testQueuedText(row.id, row.status))
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

async function loadMutes(view: GroupView): Promise<void> {
  try {
    const payload = await listQQBotGroupMutes(view.row.id)
    view.mutes = { open: view.mutes?.open ?? true, payload, error: null }
  } catch (e) {
    view.mutes = { open: view.mutes?.open ?? true, payload: null, error: errText(e) }
  }
}

function toggleMutes(view: GroupView): void {
  if (view.mutes === null) {
    view.mutes = { open: true, payload: null, error: null }
    void loadMutes(view)
    return
  }
  view.mutes.open = !view.mutes.open
}

/** 入群申请区块上屏；策略编辑器按群行（权威值）回填——照旧页 joinRequestsHtml 的输入源。 */
async function loadJoinRequests(view: GroupView): Promise<void> {
  const draft = {
    open: view.join?.open ?? true,
    mode: view.row.join_policy_mode,
    whitelistText: view.row.join_policy_whitelist.join('\n'),
  }
  try {
    const payload = await listQQBotGroupJoinRequests(view.row.id)
    view.join = { ...draft, payload, error: null }
  } catch (e) {
    view.join = { ...draft, payload: null, error: errText(e) }
  }
}

function toggleJoins(view: GroupView): void {
  if (view.join === null) {
    view.join = {
      open: true,
      payload: null,
      error: null,
      mode: view.row.join_policy_mode,
      whitelistText: view.row.join_policy_whitelist.join('\n'),
    }
    void loadJoinRequests(view)
    return
  }
  view.join.open = !view.join.open
}

async function savePolicy(view: GroupView): Promise<void> {
  const editor = view.join
  if (editor === null) return
  // select.value 是裸串：不在取值表里（被改坏的下拉）就按 off 走，⛔ 不硬塞给后端。
  const mode = (
    POLICY_MODE_OPTIONS.some(([value]) => value === editor.mode) ? editor.mode : 'off'
  ) as QQBotJoinPolicyMode
  const whitelist = mode === 'whitelist' ? parseWhitelistInput(editor.whitelistText) : []
  try {
    const saved = await updateQQBotGroupJoinPolicy(view.row.id, { mode, whitelist })
    // 群行就地更新（它带着编辑器的回填源），编辑区显示已存值——照旧页存后重渲语义。
    view.row.join_policy_mode = saved.join_policy_mode
    view.row.join_policy_whitelist = saved.join_policy_whitelist
    editor.mode = saved.join_policy_mode
    editor.whitelistText = saved.join_policy_whitelist.join('\n')
    showToast(MESSAGES.policySaved)
    await loadJoinRequests(view)
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

async function onJoinAction(
  view: GroupView,
  row: QQBotJoinRequestRow,
  op: 'approve' | 'decline',
): Promise<void> {
  let failed: string | null = null
  try {
    const result = await approveQQBotJoinRequest(view.row.id, {
      op,
      join_request_id: row.join_request_id,
      member_openid: row.member_openid,
    })
    if (!result.ok) {
      failed = `操作失败：${result.error ? remoteErrorText(result.error) : NO_REMOTE_REASON_TEXT}`
    }
  } catch (e) {
    showToast(errText(e), 'error')
    return
  }
  if (failed !== null) {
    showToast(failed, 'error')
  } else {
    showToast(op === 'approve' ? MESSAGES.joinApproved : MESSAGES.joinDeclined)
  }
  await loadJoinRequests(view)
}

function groupStatusClass(status: QQBotGroupStatus): string {
  if (status === 'active') return 'cv2-st--doing'
  if (status === 'gone') return 'cv2-st--error'
  return 'cv2-st--unassigned'
}

/* ── 区块 4：全局自定义菜单 ─────────────────────────────────────────────────── */

const menuGroupId = ref('')
const menuRows = ref<MenuTopRow[]>([])
const menuVersion = ref<number | null>(null)
const menuMsg = ref<{ bad: boolean; text: string } | null>(null)

function setMenuMsg(text: string, bad: boolean): void {
  menuMsg.value = { bad, text }
}

async function loadMenu(): Promise<void> {
  const groupId = Number(menuGroupId.value)
  if (!Number.isInteger(groupId) || groupId <= 0) {
    setMenuMsg(MESSAGES.menuPickGroup, true)
    return
  }
  try {
    const payload = await getQQBotMenu(groupId)
    if (payload.remote_error) {
      setMenuMsg(`读取菜单失败：${remoteErrorText(payload.remote_error)}`, true)
      return
    }
    const parsed = parseMenuButtons(payload.menu)
    if (parsed === null) {
      setMenuMsg(MENU_PARSE_FAIL_TEXT, true)
      return
    }
    menuRows.value = parsed
    menuVersion.value = payload.version
    setMenuMsg(`已读取当前菜单（${menuRows.value.length} 个一级）。`, false)
  } catch (e) {
    setMenuMsg(errText(e), true)
  }
}

async function saveMenu(): Promise<void> {
  const groupId = Number(menuGroupId.value)
  if (!Number.isInteger(groupId) || groupId <= 0) {
    setMenuMsg(MESSAGES.menuPickGroup, true)
    return
  }
  const errors = menuPayloadErrors(menuRows.value)
  if (errors.length > 0) {
    setMenuMsg(errors.join('；'), true)
    return
  }
  try {
    const payload = await putQQBotMenu({
      group: groupId,
      menu: { buttons: buildMenuButtons(menuRows.value) },
    })
    if (!payload.ok) {
      const reason = payload.remote_error
        ? remoteErrorText(payload.remote_error)
        : NO_REMOTE_REASON_TEXT
      setMenuMsg(`保存失败：${reason}`, true)
      return
    }
    menuVersion.value = payload.version ?? null
    setMenuMsg(menuSavedText(menuVersion.value), false)
  } catch (e) {
    setMenuMsg(errText(e), true)
  }
}

/* ── 区块 5：推送记录 ───────────────────────────────────────────────────────── */

const MESSAGE_PAGE_SIZE = 20
const msgGroupFilter = ref('')
const msgStatusFilter = ref('')
const msgPage = ref(1)
const msgPageNo = ref(1)
const msgPageSize = ref(MESSAGE_PAGE_SIZE)
const msgTotal = ref(0)
const msgRows = ref<QQBotMessageRow[]>([])
const msgError = ref<string | null>(null)

async function loadMessages(): Promise<void> {
  try {
    const page = await listQQBotMessages({
      group: msgGroupFilter.value === '' ? undefined : Number(msgGroupFilter.value),
      status:
        msgStatusFilter.value === ''
          ? undefined
          : (msgStatusFilter.value as QQBotMessageStatus),
      page: msgPage.value,
    })
    msgRows.value = page.items
    msgTotal.value = page.total
    msgPageNo.value = page.page
    msgPageSize.value = page.page_size
    msgError.value = null
  } catch (e) {
    msgRows.value = []
    msgError.value = errText(e)
  }
}

function onMsgReload(): void {
  msgPage.value = 1
  void loadMessages()
}

function onMsgPrev(): void {
  if (msgPage.value > 1) msgPage.value -= 1
  void loadMessages()
}

function onMsgNext(): void {
  if (msgPage.value * MESSAGE_PAGE_SIZE < msgTotal.value) msgPage.value += 1
  void loadMessages()
}

function msgStatusClass(status: QQBotMessageStatus): string {
  if (status === 'sent') return 'cv2-st--doing'
  if (status === 'failed') return 'cv2-st--error'
  return 'cv2-st--paused'
}

/* ── 初装与出口 ─────────────────────────────────────────────────────────────── */

onMounted(() => {
  if (!canView.value) return
  void (async () => {
    try {
      const row = await getQQBotBinding()
      applyBinding(row)
      if (row.app_id !== '' && row.app_secret_set) {
        await loadGroups()
        await loadMessages()
      }
    } catch (e) {
      showToast(errText(e), 'error')
    }
  })()
})
</script>

<template>
  <div class="cv2-page integration-form" data-v2-page="qqbot-settings">
    <template v-if="canView">
      <!-- 区块 1：机器人密钥 -->
      <section class="cv2-panel" data-v2="block-binding">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">机器人密钥</h3>
        </div>
        <div class="qb-form integration-form">
          <div class="cv2-field qb-credential-field">
            <span class="cv2-field-label">AppID</span>
            <el-input
              v-model="form.appId"
              class="qb-wide"
              maxlength="64"
              autocomplete="off"
              :placeholder="APP_ID_PLACEHOLDER"
              data-v2="app-id"
            />
          </div>
          <div class="cv2-field qb-credential-field">
            <span class="cv2-field-label">AppSecret</span>
            <el-input
              v-model="form.secret"
              class="qb-wide"
              type="password"
              autocomplete="new-password"
              maxlength="256"
              :placeholder="SECRET_PLACEHOLDER"
              data-v2="app-secret"
            />
          </div>
          <div class="cv2-field">
            <span class="cv2-field-label">已认证</span>
            <span class="qb-switchline">
              <el-switch
                v-model="form.authVerified"
                active-text="已认证"
                inactive-text="未认证"
                data-v2="auth-switch"
              />
            </span>
          </div>
          <div class="cv2-field">
            <span class="cv2-field-label">启用</span>
            <span class="qb-switchline">
              <el-switch
                v-model="form.enabled"
                active-text="启用"
                inactive-text="停用"
                data-v2="enabled-switch"
              />
            </span>
          </div>
        </div>
        <div class="qb-actions">
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="binding-save" @click="void saveBinding()">保存密钥</button>
        </div>
        <p
          v-if="bindingResult !== null"
          class="qb-line"
          :class="bindingResult.bad ? 'qb-bad' : 'qb-ok'"
          data-v2="binding-result"
        >{{ bindingResult.text }}</p>
      </section>

      <!-- 区块 2：连接状态与分享链接 -->
      <section class="cv2-panel" data-v2="block-conn">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">连接状态与分享链接</h3>
        </div>
        <p v-if="!configured" class="qb-hint qb-line" data-v2="empty-hint-conn">{{ EMPTY_HINT_TEXT }}</p>
        <template v-else>
          <div class="cv2-info-list">
            <div class="cv2-info-row" data-v2="row-bot-name">
              <span class="cv2-info-label">机器人名称</span>
              <span class="cv2-info-value" data-v2="bot-name">{{ (binding?.bot_name ?? '') === '' ? '—' : binding?.bot_name }}</span>
            </div>
            <div class="cv2-info-row" data-v2="row-conn-state">
              <span class="cv2-info-label">连接状态</span>
              <span
                class="cv2-info-value"
                :class="{ 'qb-online': binding?.connection_state === 'online' }"
                data-v2="conn-state"
              >{{ (binding?.connection_state ?? '') === '' ? '—' : binding?.connection_state }}</span>
            </div>
            <div class="cv2-info-row" data-v2="row-last-alive">
              <span class="cv2-info-label">最近心跳</span>
              <span class="cv2-info-value">{{ timeText(binding?.last_alive_at ?? null) }}</span>
            </div>
            <div class="cv2-info-row" data-v2="row-last-event">
              <span class="cv2-info-label">最近事件</span>
              <span class="cv2-info-value">{{ timeText(binding?.last_event_at ?? null) }}</span>
            </div>
          </div>
          <p v-if="binding?.markdown_ok === false" class="qb-md-warn" data-v2="markdown-warn">{{ MARKDOWN_WARN_TEXT }}</p>
          <div class="cv2-field qb-share-row" data-v2="share-row">
            <span class="cv2-field-label">分享链接</span>
            <span class="qb-share-controls">
              <select v-model="shareGroupId" class="cv2-input qb-share-select" data-v2="share-group">
                <option value="">{{ SHARE_PLACEHOLDER }}</option>
                <option v-for="g in groups" :key="g.row.id" :value="String(g.row.id)">{{ groupDisplayName(g.row) }}</option>
              </select>
              <button
                type="button"
                class="cv2-btn cv2-btn--sm cv2-btn--commit"
                data-v2="share-create"
                :disabled="groups.length === 0"
                @click="void createShare()"
              >生成分享链接</button>
              <span
                v-if="shareResult !== null"
                class="qb-share-result"
                :class="shareResult.ok ? 'qb-ok' : 'qb-bad'"
                data-v2="share-result"
              >{{ shareResult.text }}</span>
              <button
                v-if="shareUrl !== ''"
                type="button"
                class="cv2-btn cv2-btn--sm cv2-btn--quiet"
                data-v2="share-copy"
                @click="copyShare()"
              >复制链接</button>
            </span>
          </div>
        </template>
      </section>

      <!-- 区块 3：群列表 -->
      <section class="cv2-panel" data-v2="block-groups">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">群列表</h3>
        </div>
        <p v-if="!configured" class="qb-hint qb-line" data-v2="empty-hint-groups">{{ EMPTY_HINT_TEXT }}</p>
        <template v-else>
          <p v-if="groupsWarn !== ''" class="qb-bad qb-line" data-v2="groups-warn">{{ groupsWarn }}</p>
          <p v-if="groups.length === 0" class="qb-hint qb-line" data-v2="groups-empty">{{ EMPTY_GROUPS_TEXT }}</p>
          <div v-for="g in groups" :key="g.row.id" class="qb-group" :data-v2-group="g.row.id">
            <div class="qb-group-head">
              <b class="qb-group-name">{{ groupDisplayName(g.row) }}</b>
              <span class="qb-hint">openid: {{ g.row.group_openid }}</span>
              <span class="cv2-st" :class="groupStatusClass(g.row.status)" :data-v2-group-status="g.row.id">{{ g.row.status }}</span>
            </div>
            <div class="qb-group-body">
              <p class="qb-line qb-group-line">
                群名称：{{ orWhitelistPending(g.row.group_name) }} ｜ 成员数：{{ memberNumText(g.row.member_num) }} ｜ 机器人身份：{{ orWhitelistPending(g.row.member_role) }} ｜ 收消息设置：{{ g.row.recv_msg_setting === '' ? '—' : g.row.recv_msg_setting }}
              </p>
              <p class="qb-hint qb-line" :data-v2-push-summary="g.row.id">{{ pushSummaryText(g.row.push_config) }}</p>
              <div class="qb-group-actions">
                <el-input
                  v-model="g.remark"
                  class="qb-remark"
                  :placeholder="REMARK_PLACEHOLDER"
                  :data-v2-remark-input="g.row.id"
                />
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-remark-save="g.row.id" @click="void saveRemark(g)">保存备注</button>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--find" :data-v2-refresh-info="g.row.id" @click="void refreshInfo(g.row.id)">刷新群信息</button>
              </div>
              <a
                href="#"
                class="qb-toggle"
                :data-v2-group-toggle="g.row.id"
                @click.prevent="g.expanded = !g.expanded"
              >{{ g.expanded ? GROUP_TOGGLE_OPEN_TEXT : GROUP_TOGGLE_CLOSED_TEXT }}</a>
              <div v-if="g.expanded" class="qb-group-detail" :data-v2-group-detail="g.row.id">
                <p class="qb-sub-title">推送配置</p>
                <div class="qb-push-line">
                  <label class="cv2-check">
                    <input v-model="g.push.is_active" type="checkbox" :data-v2-push-active="g.row.id" /> 启用推送
                  </label>
                  <label class="cv2-check">
                    <input
                      v-model="g.push.msg_type"
                      type="radio"
                      :name="`qb-push-type-${g.row.id}`"
                      value="markdown"
                      :data-v2-push-type="`${g.row.id}-markdown`"
                    /> markdown
                  </label>
                  <label class="cv2-check">
                    <input
                      v-model="g.push.msg_type"
                      type="radio"
                      :name="`qb-push-type-${g.row.id}`"
                      value="text"
                      :data-v2-push-type="`${g.row.id}-text`"
                    /> 纯文本
                  </label>
                </div>
                <div v-if="games.length === 0" class="qb-hint qb-line" :data-v2-games-empty="g.row.id">{{ NO_GAME_TEXT }}</div>
                <div v-else class="qb-game-line">
                  <label
                    v-for="game in games"
                    :key="game.id"
                    class="cv2-check qb-game-check"
                    :data-v2-game-label="`${g.row.id}-${game.id}`"
                  >
                    <input v-model="g.push.game_ids" type="checkbox" :value="game.id" :data-v2-game-check="`${g.row.id}-${game.id}`" /> {{ game.name }}
                  </label>
                </div>
                <button
                  type="button"
                  class="cv2-btn cv2-btn--sm cv2-btn--primary qb-sub-action cv2-btn--commit"
                  :data-v2-push-save="g.row.id"
                  @click="void savePush(g)"
                >保存推送配置</button>

                <p class="qb-sub-title">测试</p>
                <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--find" :data-v2-test-push="g.row.id" @click="void testPush(g.row.id)">发送测试消息</button>

                <p class="qb-sub-title">禁言列表</p>
                <a href="#" class="qb-toggle" :data-v2-mutes-toggle="g.row.id" @click.prevent="toggleMutes(g)">{{ g.mutes?.open ? MUTES_OPEN_TEXT : MUTES_CLOSED_TEXT }}</a>
                <div v-if="g.mutes !== null && g.mutes.open" class="qb-sub-body" :data-v2-mutes-body="g.row.id">
                  <p v-if="g.mutes.error !== null" class="qb-bad qb-line">{{ MUTES_FAIL_PREFIX }}{{ g.mutes.error }}</p>
                  <template v-else-if="g.mutes.payload !== null">
                    <p v-if="g.mutes.payload.items === null" class="qb-bad qb-line">
                      {{ MUTES_FAIL_PREFIX }}{{ g.mutes.payload.remote_error ? remoteErrorText(g.mutes.payload.remote_error) : REMOTE_NONE_TEXT }}
                    </p>
                    <p v-else-if="g.mutes.payload.items.length === 0" class="qb-hint qb-line">{{ MUTES_EMPTY_TEXT }}</p>
                    <pre v-else class="qb-pre">{{ jsonText(g.mutes.payload.items) }}</pre>
                  </template>
                </div>

                <p class="qb-sub-title">入群申请</p>
                <a href="#" class="qb-toggle" :data-v2-jr-toggle="g.row.id" @click.prevent="toggleJoins(g)">{{ g.join?.open ? JR_OPEN_TEXT : JR_CLOSED_TEXT }}</a>
                <div v-if="g.join !== null && g.join.open" class="qb-sub-body" :data-v2-jr-body="g.row.id">
                  <p v-if="g.join.error !== null" class="qb-bad qb-line">{{ JR_FAIL_PREFIX }}{{ g.join.error }}</p>
                  <template v-else-if="g.join.payload !== null">
                    <p v-if="g.join.payload.items.length === 0" class="qb-hint qb-line">{{ JR_EMPTY_TEXT }}</p>
                    <div v-else class="qb-table-wrap">
                      <table class="cv2-table qb-mini-table" :data-v2-jr-table="g.row.id">
                        <thead>
                          <tr>
                            <th>申请人</th>
                            <th class="qb-th-time">申请时间</th>
                            <th class="qb-th-mid">处置结果</th>
                            <th>拒绝理由</th>
                            <th class="qb-th-mid">操作人</th>
                            <th class="qb-th-mid">操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr v-for="jr in g.join.payload.items" :key="jr.id" :data-v2-jr-row="jr.id">
                            <td>{{ jr.username === '' ? jr.member_openid : jr.username }}</td>
                            <td class="qb-nowrap">{{ timeText(jr.apply_at) }}</td>
                            <td class="qb-nowrap">{{ jr.decision }}</td>
                            <td>{{ jr.reject_reason }}</td>
                            <td class="qb-nowrap">{{ jr.acted_at === null ? '—' : (jr.acted_by_name ?? '自动') }}</td>
                            <td class="qb-nowrap">
                              <template v-if="jr.decision === 'manual_pending'">
                                <button
                                  type="button"
                                  class="cv2-btn cv2-btn--sm cv2-btn--commit"
                                  :data-v2-jr-approve="jr.join_request_id"
                                  @click="void onJoinAction(g, jr, 'approve')"
                                >通过</button>
                                <button
                                  type="button"
                                  class="cv2-btn cv2-btn--sm cv2-btn--danger qb-btn-gap"
                                  :data-v2-jr-decline="jr.join_request_id"
                                  @click="void onJoinAction(g, jr, 'decline')"
                                >拒绝</button>
                              </template>
                              <template v-else>—</template>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <div class="qb-divider" />
                    <p v-if="g.join.payload.remote_error !== undefined" class="qb-bad qb-line">
                      {{ JR_REMOTE_FAIL_PREFIX }}{{ remoteErrorText(g.join.payload.remote_error) }}
                    </p>
                    <p v-else-if="g.join.payload.remote === undefined" class="qb-hint qb-line">{{ REMOTE_BLANK_TEXT }}</p>
                    <template v-else>
                      <p class="qb-hint qb-line">{{ REMOTE_RAW_PREFIX }}</p>
                      <pre class="qb-pre">{{ jsonText(g.join.payload.remote) }}</pre>
                    </template>
                    <div class="qb-divider" />
                    <div class="cv2-field qb-policy" :data-v2-policy="g.row.id">
                      <span class="cv2-field-label">审批策略</span>
                      <span class="qb-policy-controls">
                        <select v-model="g.join.mode" class="cv2-input" :data-v2-policy-select="g.row.id">
                          <option v-for="[value, label] in POLICY_MODE_OPTIONS" :key="value" :value="value">{{ label }}</option>
                        </select>
                        <textarea
                          v-model="g.join.whitelistText"
                          class="cv2-input qb-whitelist"
                          rows="2"
                          :disabled="g.join.mode !== 'whitelist'"
                          placeholder="白名单模式的名单：一行一个成员 openid（或逗号分隔）；其它模式名单会被清空"
                          :data-v2-policy-whitelist="g.row.id"
                        />
                        <button
                          type="button"
                          class="cv2-btn cv2-btn--sm cv2-btn--primary cv2-btn--commit"
                          :data-v2-policy-save="g.row.id"
                          @click="void savePolicy(g)"
                        >保存审批策略</button>
                      </span>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </template>
      </section>

      <!-- 区块 4：全局自定义菜单 -->
      <section class="cv2-panel" data-v2="block-menu">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">全局自定义菜单</h3>
        </div>
        <p v-if="!configured" class="qb-hint qb-line" data-v2="empty-hint-menu">{{ EMPTY_HINT_TEXT }}</p>
        <template v-else>
          <div class="cv2-field" data-v2="menu-picker">
            <span class="cv2-field-label">目标群</span>
            <span class="qb-policy-controls">
              <select v-model="menuGroupId" class="cv2-input qb-share-select" data-v2="menu-group">
                <option value="">{{ SHARE_PLACEHOLDER }}</option>
                <option v-for="g in groups" :key="g.row.id" :value="String(g.row.id)">{{ groupDisplayName(g.row) }}</option>
              </select>
              <button
                type="button"
                class="cv2-btn cv2-btn--sm cv2-btn--find"
                data-v2="menu-load"
                :disabled="groups.length === 0"
                @click="void loadMenu()"
              >读取当前菜单</button>
              <span class="qb-hint">当前版本：<span data-v2="menu-version">{{ menuVersion === null ? '—' : menuVersion }}</span></span>
            </span>
          </div>
          <p v-if="menuRows.length === 0" class="qb-hint qb-line" data-v2="menu-empty">{{ MENU_EMPTY_TEXT }}</p>
          <div v-for="(row, i) in menuRows" :key="i" class="qb-menu-row" :data-v2-menu-row="i">
            <input v-model="row.name" class="cv2-input qb-menu-name" :placeholder="MENU_NAME_PLACEHOLDER" :data-v2-menu-name="i" />
            <select v-model="row.type" class="cv2-input qb-menu-type" :data-v2-menu-type="i">
              <option v-for="t in MENU_TOP_TYPES" :key="t" :value="t">{{ t }}</option>
            </select>
            <input
              v-model="row.link"
              class="cv2-input qb-menu-link"
              :placeholder="MENU_LINK_PLACEHOLDER"
              :disabled="row.type !== 'link' || row.subs.length > 0"
              :data-v2-menu-link="i"
            />
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" :data-v2-menu-addsub="i" @click="row.subs.push(emptyMenuSubRow())">+子菜单</button>
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger" :data-v2-menu-del="i" @click="menuRows.splice(i, 1)">删</button>
            <div v-for="(sub, j) in row.subs" :key="j" class="qb-menu-subrow" :data-v2-menu-subrow="`${i}-${j}`">
              <span class="qb-sub-branch">├</span>
              <input v-model="sub.name" class="cv2-input qb-sub-name" :placeholder="MENU_SUB_NAME_PLACEHOLDER" :data-v2-menu-sub-name="`${i}-${j}`" />
              <select v-model="sub.type" class="cv2-input qb-menu-type" :data-v2-menu-sub-type="`${i}-${j}`">
                <option v-for="t in MENU_SUB_TYPES" :key="t" :value="t">{{ t }}</option>
              </select>
              <input
                v-model="sub.link"
                class="cv2-input qb-sub-link"
                :placeholder="MENU_SUB_LINK_PLACEHOLDER"
                :disabled="sub.type !== 'link'"
                :data-v2-menu-sub-link="`${i}-${j}`"
              />
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--danger" :data-v2-menu-sub-del="`${i}-${j}`" @click="row.subs.splice(j, 1)">删</button>
            </div>
          </div>
          <div class="qb-actions qb-menu-actions">
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--commit" data-v2="menu-add" @click="menuRows.push(emptyMenuRow())">添加一级菜单</button>
            <button
              type="button"
              class="cv2-btn cv2-btn--sm cv2-btn--primary cv2-btn--commit"
              data-v2="menu-save"
              :disabled="groups.length === 0"
              @click="void saveMenu()"
            >保存到 QQ</button>
          </div>
          <p
            v-if="menuMsg !== null"
            class="qb-line"
            :class="menuMsg.bad ? 'qb-bad' : 'qb-ok'"
            data-v2="menu-msg"
          >{{ menuMsg.text }}</p>
        </template>
      </section>

      <!-- 区块 5：推送记录 -->
      <section class="cv2-panel" data-v2="block-messages">
        <div class="cv2-panel-head">
          <h3 class="cv2-panel-title">推送记录</h3>
        </div>
        <p v-if="!configured" class="qb-hint qb-line" data-v2="empty-hint-messages">{{ EMPTY_HINT_TEXT }}</p>
        <template v-else>
          <div class="qb-msg-filters" data-v2="msg-filters">
            <select v-model="msgGroupFilter" class="cv2-input" data-v2="msg-group-filter" @change="onMsgReload">
              <option value="">全部群</option>
              <option v-for="g in groups" :key="g.row.id" :value="String(g.row.id)">{{ groupDisplayName(g.row) }}</option>
            </select>
            <select v-model="msgStatusFilter" class="cv2-input" data-v2="msg-status-filter" @change="onMsgReload">
              <option value="">全部状态</option>
              <option value="pending">pending</option>
              <option value="sent">sent</option>
              <option value="failed">failed</option>
            </select>
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--find" data-v2="msg-reload" @click="onMsgReload">刷新</button>
          </div>
          <p v-if="msgError !== null" class="qb-bad qb-line" data-v2="msg-error">{{ msgError }}</p>
          <p v-else-if="msgRows.length === 0" class="qb-hint qb-line" data-v2="msg-empty">{{ MSG_EMPTY_TEXT }}</p>
          <div v-else class="qb-table-wrap" data-v2="msg-table">
            <table class="cv2-table qb-mini-table">
              <thead>
                <tr>
                  <th class="qb-th-time">时间</th>
                  <th>群</th>
                  <th class="qb-th-mid">类型</th>
                  <th class="qb-th-st">状态</th>
                  <th class="qb-th-mid">错误码</th>
                  <th>内容</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="m in msgRows" :key="m.id" :data-v2-msg-row="m.id">
                  <td class="qb-nowrap">{{ timeText(m.created_at) }}</td>
                  <td>{{ m.group_openid }}</td>
                  <td>{{ m.kind }}</td>
                  <td class="qb-nowrap"><span class="cv2-st" :class="msgStatusClass(m.status)" :data-v2-msg-status="m.id">{{ m.status }}</span></td>
                  <td>{{ m.error_code === '' ? '—' : m.error_code }}</td>
                  <td>{{ m.payload_text }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="qb-pager-row" data-v2="msg-pager">
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" data-v2="msg-prev" :disabled="msgPageNo <= 1" @click="onMsgPrev">上一页</button>
            <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" data-v2="msg-next" :disabled="msgPageNo * msgPageSize >= msgTotal" @click="onMsgNext">下一页</button>
            <span class="qb-hint" data-v2="msg-page-info">第 {{ msgPageNo }} 页 · 共 {{ msgTotal }} 条</span>
          </div>
        </template>
      </section>

      <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
        {{ toast.text }}
      </div>
    </template>
    <section v-else class="cv2-panel">
      <p class="cv2-empty" data-v2="no-permission">{{ NO_ACCESS_MESSAGE }}</p>
    </section>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 .ttv2 token（⛔ 不写裸色值）。 */

/* 密钥表单按空间宽度切换列数。 */
.qb-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  max-width: 880px;
  margin-top: 12px;
}

/* 压过 carbon.css 的 `.cv2-field .el-input { width:160px }`（同族特异性才盖得住）。 */
.qb-form .cv2-field .qb-wide { width: 100%; }

.qb-form .qb-credential-field { grid-template-columns: 100px minmax(0, 1fr); }
.qb-form .qb-credential-field .cv2-field-label { white-space: nowrap; }
@media (max-width: 720px) {
  .qb-form .qb-credential-field > :not(.cv2-field-label) { grid-column: 2; margin-top: 0; }
  .qb-form .qb-credential-field .cv2-field-label { border-right: none; border-radius: 4px 0 0 4px; }
  .qb-form .qb-credential-field :deep(.el-input__wrapper) { border-radius: 0 4px 4px 0; }
}

.qb-hint {
  font-size: 12px;
  color: var(--tt2-dim);
}

.qb-line {
  line-height: 24px;
}

.qb-ok {
  color: var(--tt2-st-settled);
  font-weight: 600;
}

.qb-bad {
  color: var(--tt2-st-error);
  font-weight: 600;
}

.qb-online {
  color: var(--tt2-st-settled);
  font-weight: 600;
}

.qb-switchline {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  min-width: 0;
  gap: 10px;
}

.qb-switchline :deep(.el-switch) { flex: none; white-space: nowrap; }

.qb-actions {
  margin: 12px 0;
}

/* markdown 降级黄条（tint-amber 族 callout）。 */
.qb-md-warn {
  margin: 10px 0 0;
  padding: 8px 14px;
  border: 1px solid var(--tt2-tint-amber-line);
  border-radius: 4px;
  background: var(--tt2-tint-amber-bg);
  color: var(--tt2-tint-amber);
  font-size: 13px;
}

.qb-share-row {
  margin-top: 14px;
}

.qb-share-controls {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.qb-share-select {
  width: 220px;
}

.qb-share-result {
  word-break: break-all;
}

/* ── 群卡片 ── */
.qb-group {
  margin-top: 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-panel);
  padding: 12px 14px;
}

.qb-group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px;
}

.qb-group-name {
  font-size: 14px;
}

.qb-group-body {
  margin-top: 6px;
}

.qb-group-line {
  margin: 0;
}

.qb-group-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 6px 0;
}

.qb-remark {
  width: 220px;
}

.qb-toggle {
  color: var(--tt2-primary-deep);
  font-weight: 600;
  text-decoration: none;
}

.qb-toggle:hover {
  text-decoration: underline;
}

.qb-group-detail {
  margin-top: 8px;
}

.qb-sub-title {
  margin: 12px 0 4px;
  font-weight: 700;
}

.qb-sub-action {
  margin-top: 8px;
}

.qb-push-line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-bottom: 4px;
}

.qb-game-line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 4px 0;
}

.qb-game-check {
  white-space: nowrap;
}

.qb-sub-body {
  margin-top: 4px;
}

.qb-pre {
  margin: 4px 0;
  padding: 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
  background: var(--tt2-tool-bg);
  overflow: auto;
  font-size: 12px;
  font-family: var(--tt2-mono);
}

.qb-table-wrap {
  overflow-x: auto;
}

.qb-mini-table {
  margin: 0;
}

.qb-nowrap {
  white-space: nowrap;
}

.qb-th-time {
  width: 150px;
}

.qb-th-mid {
  width: 130px;
}

.qb-th-st {
  width: 90px;
}

.qb-btn-gap {
  margin-left: 6px;
}

.qb-divider {
  border-top: 1px dashed var(--tt2-line);
  margin: 10px 0 6px;
}

.qb-policy {
  margin-bottom: 4px;
}

.qb-policy-controls {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.qb-whitelist {
  width: 420px;
}

/* ── 菜单编辑器 ── */
.qb-menu-row {
  border-bottom: 1px dashed var(--tt2-line);
  padding: 8px 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.qb-menu-name {
  width: 160px;
}

.qb-menu-type {
  width: 130px;
}

.qb-menu-link {
  width: 240px;
}

.qb-menu-subrow {
  margin-left: 24px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.qb-sub-branch {
  color: var(--tt2-dim);
}

.qb-sub-name {
  width: 140px;
}

.qb-sub-link {
  width: 220px;
}

.qb-menu-actions {
  display: flex;
  gap: 8px;
}

/* ── 推送记录 ── */
.qb-msg-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 12px 0 8px;
}

.qb-msg-filters .cv2-input {
  width: 180px;
}

.qb-pager-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

/* 炭火 toast：居中深条（人员列表同款口径）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}
</style>

<style scoped src="./integration-form.css"></style>

<style scoped>
@media (max-width: 720px) {
  .qb-form { grid-template-columns: minmax(0, 1fr); }
}
</style>
