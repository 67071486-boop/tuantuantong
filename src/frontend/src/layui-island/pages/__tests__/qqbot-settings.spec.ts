// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import type { ConfigRow } from '@/api/config'
import type {
  QQBotBinding,
  QQBotGroupRow,
  QQBotJoinRequestRow,
  QQBotMessageRow,
} from '@/api/qqbot'

/**
 * 【第三方平台 → QQ机器人设置】island 页的契约测试（QQ 机器人线 批次C，2026-10-19）。
 *
 * 这一页是**自有功能**（对标物里没有机器人对接），所以没有证据件可对拍；本文件钉的是
 * **有真实后果**的约定（手法照抄 `__tests__/wecom-group-qr.spec.ts`，⛔ 不自己发明一套）：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    ①保存密钥（secret 留空**不发该字段**）+ 验真两条分支；②群列表渲染（白名单未批占位）+
 *    推送配置勾游戏保存；③测试推送（失败把后端文案说出来）；④菜单编辑器增删行 + 上限 +
 *    https + 保存载荷；⑤推送记录状态徽标；⑥入群申请通过/拒绝、「操作人」列三形态
 *    （评审 P2-3：—/显示名/自动）与审批策略载荷；⑦评审 P1-2：刷新后回填已存名单；
 *    ⑧评审 P3-⑤：密钥区「改凭据需重启 qqbot_ws」灰字。
 * 2. **三条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量（标记里不许出现 `class=".qqbotCard"`）；
 *    挂载顺序以**运行期 DOM** 判断（bootstrapIsland 看不到标记、initIslandElements 看得到）；
 *    各用例自己 `document.body.innerHTML = ''` 收口（本页没有定时器）。
 * 3. ⭐ **页内自检那道门**：非账号管理员（哪怕**持有**那个节点）深链打开 ⇒ `msg` 一句无权 +
 *    **不注入标记** + **一个请求都不发**（fail-closed，照 wecom-group-qr 的 D10 同款）。
 * 4. ⭐ **节点码住在 api 模块**（`tests/test_rbac_menu_alignment.py` 只扫 `pages/*.ts` 的带点
 *    字面量；先例 `taker-apps` / `open-apps`）⇒ 本文件钉它的**字面值**。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：AppID / openid / 群名都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const opens: Array<Record<string, unknown>> = []
  const formRenders: Array<{ type?: string; filter?: string }> = []
  // ⚠️ 挂载顺序的**运行期**判据（§5-G 陷阱 ③）。⚠️ 选择器写字面值：本工厂在页面模块求值前就跑。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    opens,
    formRenders,
    seenByBootstrap,
    seenByInit,
    layer: {
      open: vi.fn((o: Record<string, unknown>) => {
        opens.push(o)
        return 1
      }),
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
      confirm: vi.fn(),
    },
    form: {
      render: vi.fn((type?: string, filter?: string) => {
        formRenders.push({ type, filter })
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#qqbotBindingSaveBtn') !== null)
    return { layer: LAYUI.layer, form: LAYUI.form }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#qqbotBindingSaveBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见上）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

/** 身份：本页第一句就 `fetchMe()`（自检那道门）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  APP_ID_INPUT_ID,
  BINDING_RESULT_ID,
  BINDING_SAVE_BUTTON_ID,
  CONFIGURED_ONLY_SELECTOR,
  EMPTY_HINT_SELECTOR,
  EMPTY_HINT_TEXT,
  GAME_CHECK_CLASS,
  GROUP_DETAIL_CLASS,
  GROUP_TOGGLE_CLASS,
  GROUPS_BODY_ID,
  JR_BODY_CLASS,
  JR_TOGGLE_CLASS,
  MENU_ADD_BUTTON_ID,
  MENU_GROUP_SELECT_ID,
  MENU_LOAD_BUTTON_ID,
  MENU_MSG_ID,
  MENU_ROWS_ID,
  MENU_SAVE_BUTTON_ID,
  MESSAGES,
  MSG_BODY_ID,
  MSG_GROUP_FILTER_ID,
  MSG_STATUS_FILTER_ID,
  NO_ACCESS_MESSAGE,
  PUSH_SAVE_CLASS,
  REFRESH_INFO_CLASS,
  SECRET_INPUT_ID,
  SHARE_BUTTON_ID,
  SHARE_COPY_BUTTON_ID,
  SHARE_GROUP_SELECT_ID,
  SHARE_RESULT_ID,
  TEST_PUSH_CLASS,
  WHITELIST_INPUT_CLASS,
  __testables,
} from '../qqbot-settings'

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这四个）。 */
type Cfg = { method?: string; url?: string; data?: unknown; params?: Record<string, unknown> }

/**
 * 响应路由：按 `METHOD URL` 认领（query 在 `params` 里、不进 key）。**没有备好响应就抛**
 * （⛔ 不是静默回 undefined——那会让"页面根本没发这条请求"与"发了但没人管"长得一样）。
 */
let routes: Record<string, (data?: unknown, cfg?: Cfg) => unknown>

function ready(key: string, fn: (data?: unknown, cfg?: Cfg) => unknown): void {
  routes[key] = fn
}

// ── 夹具（**全部合成**）────────────────────────────────────────────────────

/** 已配置好的凭据单例（secret 只回答「配没配」）。 */
const BINDING: QQBotBinding = {
  app_id: 'synthetic-app-id',
  enabled: true,
  auth_verified: true,
  bot_name: '合成机器人',
  markdown_ok: true,
  connection_state: 'online',
  last_alive_at: '2026-10-19T10:00:00+08:00',
  last_event_at: '2026-10-19T10:01:00+08:00',
  last_error: '',
  menu_version: 2,
  app_secret_set: true,
}

/** 「还没配」的单例（空行：`get_solo` 的缺省形态）。 */
const UNCONFIGURED: QQBotBinding = {
  app_id: '',
  enabled: false,
  auth_verified: false,
  bot_name: '',
  markdown_ok: true,
  connection_state: '',
  last_alive_at: null,
  last_event_at: null,
  last_error: '',
  menu_version: null,
  app_secret_set: false,
}

/** 群 A：官方资料**没拿到**（白名单接口未批的典型形态）⇒ 三个格子都显示「白名单未批」占位。 */
const GROUP_A: QQBotGroupRow = {
  id: 5,
  group_openid: 'synthetic-group-openid-A',
  remark: '',
  group_name: '',
  member_num: null,
  member_role: '',
  allow_proactive_msg: null,
  recv_msg_setting: '',
  status: 'active',
  first_seen_at: '2026-10-19T09:00:00+08:00',
  last_seen_at: '2026-10-19T10:00:00+08:00',
  join_policy_mode: 'off',
  join_policy_whitelist: [],
  push_config: null,
}

/** 群 B：资料齐全、已配推送；**白名单模式且已存名单**（P1-2：刷新后要能回填）。 */
const GROUP_B: QQBotGroupRow = {
  id: 6,
  group_openid: 'synthetic-group-openid-B',
  remark: '备注B',
  group_name: '合成群B',
  member_num: 12,
  member_role: 'administrator',
  allow_proactive_msg: true,
  recv_msg_setting: 'accept',
  status: 'gone',
  first_seen_at: '2026-10-18T09:00:00+08:00',
  last_seen_at: '2026-10-19T09:30:00+08:00',
  join_policy_mode: 'whitelist',
  join_policy_whitelist: ['synthetic-member-kept'],
  push_config: { is_active: true, msg_type: 'markdown', game_ids: [101] },
}

const GAMES: ConfigRow[] = [
  { id: 101, name: '合成游戏A', sort_order: 1, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
  { id: 102, name: '合成游戏B', sort_order: 2, is_active: true, created_at: '2026-10-01T00:00:00+08:00' },
]

/** 入群申请本地留痕三条：待人工（未处置 ⇒ 操作人「—」、操作列有按钮）、
 * 人工通过（⭐ P2-3：带操作人显示名）、自动通过（⭐ P2-3：无操作人 ⇒ 显示「自动」）。 */
const JOIN_ROWS: QQBotJoinRequestRow[] = [
  {
    id: 11,
    join_request_id: 'synthetic-jr-1',
    member_openid: 'synthetic-member-1',
    username: '合成申请人',
    apply_at: '2026-10-19T09:40:00+08:00',
    apply_source: '',
    risk_tips: '',
    verify_info: null,
    decision: 'manual_pending',
    reject_reason: '',
    error_code: '',
    acted_at: null,
    acted_by: null,
    acted_by_name: null,
    created_at: '2026-10-19T09:40:00+08:00',
  },
  {
    id: 12,
    join_request_id: 'synthetic-jr-2',
    member_openid: 'synthetic-member-2',
    username: '已通过的人',
    apply_at: '2026-10-19T09:00:00+08:00',
    apply_source: '',
    risk_tips: '',
    verify_info: null,
    decision: 'manual_approved',
    reject_reason: '',
    error_code: '',
    acted_at: '2026-10-19T09:05:00+08:00',
    acted_by: 1,
    acted_by_name: '合成管理员',
    created_at: '2026-10-19T09:00:00+08:00',
  },
  {
    id: 13,
    join_request_id: 'synthetic-jr-3',
    member_openid: 'synthetic-member-3',
    username: '白名单命中的人',
    apply_at: '2026-10-19T08:00:00+08:00',
    apply_source: '',
    risk_tips: '',
    verify_info: null,
    decision: 'auto_approved',
    reject_reason: '',
    error_code: '',
    acted_at: '2026-10-19T08:00:01+08:00',
    acted_by: null,
    acted_by_name: null,
    created_at: '2026-10-19T08:00:00+08:00',
  },
]

/** 推送记录三行（**三种状态各一条**，判据要能分辨它们）。 */
const MSG_SENT: QQBotMessageRow = {
  id: 61,
  group_id: 5,
  group_openid: 'synthetic-group-openid-A',
  kind: 'test_push',
  order_id: null,
  msg_type_eff: 'text',
  payload_text: '合成测试消息',
  status: 'sent',
  attempts: 1,
  error_code: '',
  error_text: '',
  scheduled_at: '2026-10-19T10:00:00+08:00',
  sent_at: '2026-10-19T10:00:05+08:00',
  created_at: '2026-10-19T10:00:00+08:00',
}
const MSG_FAILED: QQBotMessageRow = {
  ...MSG_SENT,
  id: 62,
  status: 'failed',
  attempts: 7,
  error_code: '11244',
  error_text: '官方拒了',
}
const MSG_PENDING: QQBotMessageRow = {
  ...MSG_SENT,
  id: 63,
  status: 'pending',
  attempts: 0,
}

function flush(): Promise<void> {
  // 页面的取数链是**多跳**的（点击 → request → 再 request → 上屏）：连放几轮宏任务。
  return (async () => {
    for (let i = 0; i < 6; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

/** 最近一次发往某条 URL 的请求（没有就抛，避免静默空判据）。 */
function callTo(method: string, url: string): Cfg {
  const found = mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method && c.url === url)
  if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
  return found[found.length - 1]!
}

/** 打到某条 URL 的请求条数（用于"重新拉了一次"这种判据）。 */
function callCount(method: string, url: string): number {
  return mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method && c.url === url).length
}

describe('QQ机器人设置 island 页（qqbot-settings）', () => {
  let root: HTMLElement
  /** 服务端**此刻**的凭据单例 / 群清单。 */
  let bindingOnServer: QQBotBinding
  let groupsOnServer: QQBotGroupRow[]

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
    LAYUI.opens.length = 0
    LAYUI.formRenders.length = 0
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    mockedFetchMe.mockResolvedValue({
      id: 1,
      username: 'synthetic-admin',
      display_name: '合成管理员',
      identity_plane: 'tenant',
      is_tenant_admin: true,
      is_account_admin: true,
      roles: [],
      menu_permissions: ['system.qqbot_settings'],
      permissions: [],
    })

    bindingOnServer = BINDING
    groupsOnServer = [GROUP_A, GROUP_B]
    routes = {}
    ready('GET /api/qqbot/binding/', () => bindingOnServer)
    // PATCH 把入参**并回**那一行；带了凭据格且凭据齐 ⇒ 服务端试取 token、响应多 `verified`。
    ready('PATCH /api/qqbot/binding/', (data) => {
      const payload = (data ?? {}) as Partial<QQBotBinding>
      bindingOnServer = { ...bindingOnServer, ...payload }
      const touched = 'app_id' in payload || 'app_secret' in payload
      const readyCreds =
        bindingOnServer.app_id !== '' && bindingOnServer.app_secret_set
      return touched && readyCreds ? { ...bindingOnServer, verified: true } : bindingOnServer
    })
    ready('GET /api/qqbot/groups/', () => ({ items: groupsOnServer }))
    ready('PATCH /api/qqbot/groups/5/', (data) => {
      const payload = (data ?? {}) as Partial<QQBotGroupRow>
      const found = groupsOnServer.find((g) => g.id === 5)
      if (!found) throw new Error('群 5 不在夹具里')
      const updated = { ...found, ...payload }
      groupsOnServer = groupsOnServer.map((g) => (g.id === 5 ? updated : g))
      return updated
    })
    ready('PUT /api/qqbot/groups/5/push-config/', (data) => {
      const payload = data as { is_active: boolean; msg_type: string; game_ids: number[] }
      const found = groupsOnServer.find((g) => g.id === 5)
      if (!found) throw new Error('群 5 不在夹具里')
      const updated = {
        ...found,
        push_config: {
          is_active: payload.is_active,
          msg_type: payload.msg_type as 'markdown' | 'text',
          game_ids: payload.game_ids,
        },
      }
      groupsOnServer = groupsOnServer.map((g) => (g.id === 5 ? updated : g))
      return { group_id: 5, ...payload }
    })
    ready('POST /api/qqbot/groups/5/test-push/', () => ({ id: 77, status: 'pending' }))
    ready('GET /api/qqbot/groups/5/mutes/', () => ({ items: [] }))
    ready('GET /api/qqbot/groups/5/join-requests/', () => ({
      items: JOIN_ROWS,
      remote: { items: [], total: 0 },
    }))
    ready('POST /api/qqbot/groups/5/join-requests/approve/', () => ({
      ok: true,
      decision: 'manual_approved',
      error: null,
    }))
    ready('PATCH /api/qqbot/groups/5/join-policy/', (data) => {
      const payload = data as { mode: string; whitelist?: string[] }
      // 照实镜像后端：PATCH join-policy **会更新群行**（P1-2 起群行带名单，两者同源）。
      const found = groupsOnServer.find((g) => g.id === 5)
      if (!found) throw new Error('群 5 不在夹具里')
      const updated = {
        ...found,
        join_policy_mode: payload.mode as QQBotGroupRow['join_policy_mode'],
        join_policy_whitelist: payload.mode === 'whitelist' ? (payload.whitelist ?? []) : [],
      }
      groupsOnServer = groupsOnServer.map((g) => (g.id === 5 ? updated : g))
      return {
        group_id: 5,
        join_policy_mode: updated.join_policy_mode,
        join_policy_whitelist: updated.join_policy_whitelist,
      }
    })
    ready('GET /api/qqbot/menu/', () => ({
      menu: {
        buttons: [
          { name: '官网', type: 'link', link: 'https://menu.example.com' },
          { name: '服务', sub_button: [{ name: '联系客服', type: 'send_message' }] },
        ],
      },
      version: 3,
    }))
    ready('PUT /api/qqbot/menu/', () => ({ ok: true, version: 4 }))
    ready('POST /api/qqbot/share-link/', () => ({
      url: 'https://synthetic.qq/share/link',
    }))
    ready('GET /api/qqbot/messages/', () => ({
      items: [MSG_SENT, MSG_FAILED, MSG_PENDING],
      total: 3,
      page: 1,
      page_size: 20,
    }))
    ready('GET /api/business/games/', () => ({
      items: GAMES,
      total: GAMES.length,
      page: 1,
      page_size: 200,
    }))
    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url ?? ''}`
        const fn = routes[key]
        if (!fn) throw new Error(`测试没有为 ${key} 准备响应`)
        return fn(cfg.data, cfg)
      }),
    )
  })

  afterEach(() => {
    // 每个用例自己收口（§5-G 陷阱 ④：上一个用例挂载的页面会继续打**同一个**替身）。
    document.body.innerHTML = ''
  })

  async function mount(): Promise<void> {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  }

  /** 页面上某个节点。 */
  function at<T extends HTMLElement>(id: string): T {
    const el = root.querySelector<T>(`#${id}`)
    if (!el) throw new Error(`页面上没有 #${id}`)
    return el
  }

  /** 按 id 取群卡片。 */
  function groupCard(groupId: number): HTMLElement {
    const card = root.querySelector<HTMLElement>(`[data-group-id="${groupId}"]`)
    if (!card) throw new Error(`页面上没有群卡片 ${groupId}`)
    return card
  }

  function stubClipboard(): ReturnType<typeof vi.fn> {
    const writeText = vi.fn((_text: string) => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    return writeText
  }

  // ── 挂载顺序（陷阱 ③，运行期判据）与标记形状 ────────────────────────────────

  it('先挂样式再塞标记：bootstrapIsland 看不到标记、initIslandElements 看得到', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('类名与选择器是两个常量：五个区块卡片 + 关键节点都在（⛔ 不是 `class=".qqbotCard"`）', async () => {
    await mount()
    expect(root.innerHTML).not.toContain('class=".qqbotCard"')
    // 五个区块（密钥 / 连接与分享 / 群列表 / 菜单 / 推送记录）各一张**顶层**卡片
    // （群卡片也带 CARD_CLASS，但它们渲染在群列表卡片**里面**，不是 root 的直接子节点）
    // ——少一个就是"点了没反应"。
    const topCards = Array.from(root.children).filter((el) => el.classList.contains('qqbotCard'))
    expect(topCards).toHaveLength(5)
    for (const id of [
      APP_ID_INPUT_ID,
      SECRET_INPUT_ID,
      BINDING_SAVE_BUTTON_ID,
      SHARE_GROUP_SELECT_ID,
      SHARE_BUTTON_ID,
      GROUPS_BODY_ID,
      MENU_GROUP_SELECT_ID,
      MENU_ADD_BUTTON_ID,
      MENU_SAVE_BUTTON_ID,
      MSG_BODY_ID,
      MSG_GROUP_FILTER_ID,
      MSG_STATUS_FILTER_ID,
    ]) {
      expect(root.querySelector(`#${id}`), `标记里缺 #${id}`).not.toBeNull()
    }
    // 两颗开关按 layui 的用法渲染：`form.render("checkbox", <filter>)` 被调到（否则是原生复选框）。
    expect(LAYUI.formRenders.length).toBeGreaterThan(0)
    expect(LAYUI.formRenders[0]).toEqual({ type: 'checkbox', filter: 'qqbotBindingForm' })
    // ⭐ 评审 P3-⑤：密钥区的灰字提示（qqbot_ws 是常驻进程，改凭据/认证状态不重启不生效）。
    expect(root.textContent).toContain('修改凭据 / 认证状态后需重启 qqbot_ws 生效')
  })

  // ── 空态：机器人没配置 ⇒ 2–5 区显示引导、不发取数请求 ────────────────────────

  it('⭐ 未配置机器人 ⇒ 2–5 区显示「先在上方保存密钥」引导、内容收起；除 binding 外零请求', async () => {
    bindingOnServer = UNCONFIGURED
    await mount()
    const hints = root.querySelectorAll<HTMLElement>(EMPTY_HINT_SELECTOR)
    expect(hints.length).toBe(4)
    for (const hint of hints) {
      expect(hint.style.display).toBe('')
      expect(hint.textContent).toBe(EMPTY_HINT_TEXT)
    }
    for (const content of root.querySelectorAll<HTMLElement>(CONFIGURED_ONLY_SELECTOR)) {
      expect(content.style.display).toBe('none')
    }
    const methods = mockedRequest.mock.calls.map((c) => (c[0] as Cfg).method?.toLowerCase())
    expect(methods).toEqual(['get'])
    expect(callTo('get', '/api/qqbot/binding/')).toBeTruthy()
  })

  // ── 已配置：群列表渲染（含「白名单未批」占位）───────────────────────────────

  it('已配置 ⇒ 群卡片渲染；资料没拿到的格子显示「白名单未批」占位（⛔ 不是 0 / 空白）', async () => {
    await mount()
    const cardA = groupCard(5)
    expect(cardA.textContent).toContain('白名单未批')
    expect(cardA.textContent).not.toContain('成员数：0')
    expect(cardA.textContent).toContain('active')
    const cardB = groupCard(6)
    expect(cardB.textContent).toContain('合成群B')
    expect(cardB.textContent).toContain('12')
    expect(cardB.textContent).toContain('gone')
    // 推送摘要：群 B 已配置 ⇒ 显示摘要而不是「未配置」。
    expect(cardB.textContent).toContain('推送已启用')
    expect(cardB.textContent).toContain('限定游戏 1 个')
    // 三个群下拉都被填上（占位/全部项 + 2 个群）；记录筛选的空值项是「全部群」。
    expect(at<HTMLSelectElement>(SHARE_GROUP_SELECT_ID).querySelectorAll('option')).toHaveLength(3)
    expect(at<HTMLSelectElement>(MENU_GROUP_SELECT_ID).querySelectorAll('option')).toHaveLength(3)
    const filterOptions = at<HTMLSelectElement>(MSG_GROUP_FILTER_ID).querySelectorAll('option')
    expect(filterOptions).toHaveLength(3)
    expect(filterOptions[0]!.textContent).toBe('全部群')
  })

  // ── ① 保存密钥：载荷正确 + 验真两条分支 ────────────────────────────────────

  it('⭐ 保存密钥：secret 留空**不发该字段**；填了就发；验真通过显示绿字', async () => {
    await mount()
    // ① 换 secret：PATCH 的 body 里**有** app_secret。
    at<HTMLInputElement>(SECRET_INPUT_ID).value = 'synthetic-new-secret'
    at<HTMLButtonElement>(BINDING_SAVE_BUTTON_ID).click()
    await flush()
    const withSecret = callTo('patch', '/api/qqbot/binding/').data as Record<string, unknown>
    expect(withSecret).toEqual({
      app_id: BINDING.app_id,
      enabled: true,
      auth_verified: true,
      app_secret: 'synthetic-new-secret',
    })
    expect(at(BINDING_RESULT_ID).textContent).toContain('验真通过')

    // ② secret 清空再保存：**没有** app_secret 这个键（留空 = 不改，⛔ 不许发空串去"清掉"）。
    at<HTMLInputElement>(SECRET_INPUT_ID).value = ''
    at<HTMLButtonElement>(BINDING_SAVE_BUTTON_ID).click()
    await flush()
    const withoutSecret = callTo('patch', '/api/qqbot/binding/').data as Record<string, unknown>
    expect('app_secret' in withoutSecret).toBe(false)
    expect(withoutSecret.app_id).toBe(BINDING.app_id)
  })

  it('⭐ 验真失败分支：verified=false ⇒ 红条显示后端的 last_error（配置仍已保存的语义说清楚）', async () => {
    await mount()
    ready('PATCH /api/qqbot/binding/', () => ({
      ...BINDING,
      last_error: 'TOKEN: 10003 invalid app_secret',
      verified: false,
    }))
    at<HTMLInputElement>(SECRET_INPUT_ID).value = 'synthetic-bad-secret'
    at<HTMLButtonElement>(BINDING_SAVE_BUTTON_ID).click()
    await flush()
    const result = at(BINDING_RESULT_ID)
    expect(result.textContent).toContain('验真失败')
    expect(result.textContent).toContain('TOKEN: 10003 invalid app_secret')
    // 配置**仍已保存**（用户可能离线填好再连）——这一句要说破，⛔ 不是只报错。
    expect(result.textContent).toContain('已保存')
  })

  // ── ② 推送配置：勾游戏保存 → PUT 载荷 ─────────────────────────────────────

  it('⭐ 推送配置：展开 → 勾游戏 101、切纯文本 → PUT 整份替换载荷（三格都在）', async () => {
    await mount()
    const card = groupCard(5)
    // 展开区默认收起；点一下展开。
    const detail = card.querySelector<HTMLElement>(`.${GROUP_DETAIL_CLASS}`)
    expect(detail?.style.display).toBe('none')
    card.querySelector<HTMLElement>(`.${GROUP_TOGGLE_CLASS}`)!.click()
    expect(detail?.style.display).toBe('')

    // 勾游戏 101（102 不勾）、切「纯文本」；开关保持默认的启用。
    const check101 = card.querySelector<HTMLInputElement>(
      `.${GAME_CHECK_CLASS}[data-game-id="101"]`,
    )
    expect(check101).not.toBeNull()
    check101!.checked = true
    const textRadio = card.querySelector<HTMLInputElement>('input[value="text"]')
    expect(textRadio).not.toBeNull()
    textRadio!.checked = true

    card.querySelector<HTMLElement>(`.${PUSH_SAVE_CLASS}`)!.click()
    await flush()
    expect(callTo('put', '/api/qqbot/groups/5/push-config/').data).toEqual({
      is_active: true,
      msg_type: 'text',
      game_ids: [101],
    })
    // 保存后重新拉了一次群清单（摘要行会更新成「限定游戏 1 个」）。
    expect(callCount('get', '/api/qqbot/groups/')).toBe(2)
  })

  // ── ③ 测试推送：POST + 失败把后端文案说出来 ────────────────────────────────

  it('⭐ 测试推送：POST groups/5/test-push/；成功提示已入队，失败**如实显示**后端文案', async () => {
    await mount()
    groupCard(5).querySelector<HTMLElement>(`.${TEST_PUSH_CLASS}`)!.click()
    await flush()
    expect(callTo('post', '/api/qqbot/groups/5/test-push/')).toBeTruthy()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('已入队')
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('#77')

    ready('POST /api/qqbot/groups/5/test-push/', () => {
      throw new BizError(1030, '机器人未配置：请先在 QQ 机器人设置页填写 AppID / AppSecret')
    })
    groupCard(5).querySelector<HTMLElement>(`.${TEST_PUSH_CLASS}`)!.click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(
      '机器人未配置：请先在 QQ 机器人设置页填写 AppID / AppSecret',
    )
  })

  // ── ④ 菜单编辑器：增删行 / 上限 / https / 保存载荷 ─────────────────────────

  it('⭐ 菜单编辑器：读取回填 → 增删行 → 保存载荷（link 带 https 值；version 上屏）', async () => {
    await mount()
    at<HTMLSelectElement>(MENU_GROUP_SELECT_ID).value = '6'
    at<HTMLButtonElement>(MENU_LOAD_BUTTON_ID).click()
    await flush()
    expect(callTo('get', '/api/qqbot/menu/').params).toEqual({ group: 6 })
    // 读取回填：官方回包的两行进编辑器（link 行 + 文件夹行）。
    expect(root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)).toHaveLength(2)

    // ① 删掉第二行（文件夹行）⇒ 剩 1 行。
    const rows = () => root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)
    rows()[1]!.querySelector<HTMLElement>('.qqbotMenuDel')!.click()
    await flush()
    expect(rows()).toHaveLength(1)

    // ② 再加一行，把第一行改成 link + 合法 https。
    at<HTMLButtonElement>(MENU_ADD_BUTTON_ID).click()
    await flush()
    expect(rows()).toHaveLength(2)
    const first = rows()[0]!
    first.querySelector<HTMLInputElement>('.qqbotMenuName')!.value = '官网'
    first.querySelector<HTMLInputElement>('.qqbotMenuName')!.dispatchEvent(new Event('input', { bubbles: true }))
    const typeSelect = first.querySelector<HTMLSelectElement>('.qqbotMenuType')!
    typeSelect.value = 'link'
    typeSelect.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    // change 触发重渲 ⇒ 重新取第一行的节点。
    const firstAfter = root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)[0]!
    firstAfter.querySelector<HTMLInputElement>('.qqbotMenuValue')!.value = 'https://menu.example.com'
    firstAfter.querySelector<HTMLInputElement>('.qqbotMenuValue')!.dispatchEvent(new Event('input', { bubbles: true }))
    // 第二行保持空名 ⇒ 先按一次保存，被「缺少名称」挡住（⛔ 不发 PUT）。
    at<HTMLButtonElement>(MENU_SAVE_BUTTON_ID).click()
    await flush()
    expect(at(MENU_MSG_ID).textContent).toContain('缺少名称')
    expect(mockedRequest.mock.calls.some((c) => (c[0] as Cfg).method === 'put')).toBe(false)

    // ③ 把第二行补上名字 ⇒ 保存成功：PUT 载荷 = 编辑器状态（group 键 + buttons）。
    const second = root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)[1]!
    second.querySelector<HTMLInputElement>('.qqbotMenuName')!.value = '客服'
    second.querySelector<HTMLInputElement>('.qqbotMenuName')!.dispatchEvent(new Event('input', { bubbles: true }))
    at<HTMLButtonElement>(MENU_SAVE_BUTTON_ID).click()
    await flush()
    expect(callTo('put', '/api/qqbot/menu/').data).toEqual({
      group: 6,
      menu: {
        buttons: [
          { name: '官网', type: 'link', link: 'https://menu.example.com' },
          { name: '客服', type: 'switch' },
        ],
      },
    })
    expect(at(MENU_MSG_ID).textContent).toContain('菜单已保存，版本 4')
  })

  it('⭐ 菜单上限与 https：一级 >10 拦下；link 非 https 拦下（都不发 PUT）', async () => {
    await mount()
    at<HTMLSelectElement>(MENU_GROUP_SELECT_ID).value = '6'
    for (let i = 0; i < 11; i += 1) at<HTMLButtonElement>(MENU_ADD_BUTTON_ID).click()
    await flush()
    expect(root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)).toHaveLength(11)
    at<HTMLButtonElement>(MENU_SAVE_BUTTON_ID).click()
    await flush()
    expect(at(MENU_MSG_ID).textContent).toContain('一级菜单最多 10 个')
    expect(mockedRequest.mock.calls.some((c) => (c[0] as Cfg).method === 'put')).toBe(false)

    // https 校验（前端同一条判据先拦；后端仍是最终裁决者）：把行删到只剩 1 行。
    let rowEls = root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)
    while (rowEls.length > 1) {
      rowEls[rowEls.length - 1]!.querySelector<HTMLElement>('.qqbotMenuDel')!.click()
      await flush()
      rowEls = root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)
    }
    const only = rowEls[0]! as HTMLElement
    only.querySelector<HTMLInputElement>('.qqbotMenuName')!.value = '官网'
    only.querySelector<HTMLInputElement>('.qqbotMenuName')!.dispatchEvent(new Event('input', { bubbles: true }))
    const typeSelect = only.querySelector<HTMLSelectElement>('.qqbotMenuType')!
    typeSelect.value = 'link'
    typeSelect.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    const onlyAfter = root.querySelectorAll(`#${MENU_ROWS_ID} .qqbotMenuRow`)[0]!
    onlyAfter.querySelector<HTMLInputElement>('.qqbotMenuValue')!.value = 'http://evil.example'
    onlyAfter.querySelector<HTMLInputElement>('.qqbotMenuValue')!.dispatchEvent(new Event('input', { bubbles: true }))
    at<HTMLButtonElement>(MENU_SAVE_BUTTON_ID).click()
    await flush()
    expect(at(MENU_MSG_ID).textContent).toContain('https')
    expect(mockedRequest.mock.calls.some((c) => (c[0] as Cfg).method === 'put')).toBe(false)
  })

  // ── ⑤ 推送记录：状态徽标渲染 + 筛选 ───────────────────────────────────────

  it('⭐ 推送记录：三种状态徽标都渲染出来；群筛选真的进了查询参数', async () => {
    await mount()
    const body = at(MSG_BODY_ID)
    for (const status of ['sent', 'failed', 'pending']) {
      expect(body.textContent).toContain(status)
    }
    // 徽标是**按状态分色**的（判据打在标记上：三种颜色都在，色值来自 `messageStatusColor`）。
    expect(body.innerHTML).toContain(__testables.messageStatusColor('sent'))
    expect(body.innerHTML).toContain(__testables.messageStatusColor('failed'))
    expect(body.innerHTML).toContain(__testables.messageStatusColor('pending'))
    // 失败行的错误码列原样显示。
    expect(body.textContent).toContain('11244')

    // 筛选：切到群 6 + failed ⇒ 参数里真的带上（⛔ 不是"看起来能选"）。
    at<HTMLSelectElement>(MSG_GROUP_FILTER_ID).value = '6'
    at<HTMLSelectElement>(MSG_GROUP_FILTER_ID).dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    at<HTMLSelectElement>(MSG_STATUS_FILTER_ID).value = 'failed'
    at<HTMLSelectElement>(MSG_STATUS_FILTER_ID).dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    expect(callTo('get', '/api/qqbot/messages/').params).toEqual({ group: 6, status: 'failed', page: 1 })
  })

  it('推送记录空列表说「还没有推送记录」（⛔ 不渲染成空表格）', async () => {
    ready('GET /api/qqbot/messages/', () => ({ items: [], total: 0, page: 1, page_size: 20 }))
    await mount()
    expect(at(MSG_BODY_ID).textContent).toContain('还没有推送记录')
  })

  // ── 禁言列表 / 入群申请：空 / 降级 / 审批载荷 ───────────────────────────────

  it('禁言列表：空显示文案；降级（items=null）显示 remote_error 原文（⛔ 不互相冒充）', async () => {
    await mount()
    const card = groupCard(5)
    card.querySelector<HTMLElement>('.qqbotMutesToggle')!.click()
    await flush()
    const body = card.querySelector<HTMLElement>('.qqbotMutesBody')!
    expect(body.textContent).toContain('当前没有禁言中的成员')

    ready('GET /api/qqbot/groups/5/mutes/', () => ({
      items: null,
      remote_error: { code: '11244', message: '没有禁言查询权限' },
    }))
    card.querySelector<HTMLElement>('.qqbotMutesToggle')!.click() // 收起
    card.querySelector<HTMLElement>('.qqbotMutesToggle')!.click() // 再展开（重新拉取）
    await flush()
    expect(body.textContent).toContain('禁言列表拉取失败')
    expect(body.textContent).toContain('没有禁言查询权限')
  })

  it('⭐ 入群申请：待人工那行有通过/拒绝；通过 → POST 载荷正确；策略保存按模式发名单', async () => {
    await mount()
    const card = groupCard(5)
    card.querySelector<HTMLElement>(`.${JR_TOGGLE_CLASS}`)!.click()
    await flush()
    const body = card.querySelector<HTMLElement>(`.${JR_BODY_CLASS}`)!
    // 本地留痕三行都在；操作列只给待人工那行画按钮（已处置的行是「—」）。
    expect(body.textContent).toContain('合成申请人')
    expect(body.textContent).toContain('manual_pending')
    expect(body.textContent).toContain('manual_approved')
    expect(body.querySelectorAll('.qqbotJrApprove')).toHaveLength(1)
    expect(body.querySelectorAll('.qqbotJrDecline')).toHaveLength(1)
    // ⭐ 评审 P2-3：「操作人」列三种形态各一行——未处置「—」/ 人工显示名 / 自动「自动」。
    const jrRows = body.querySelectorAll('tbody tr')
    expect(jrRows).toHaveLength(3)
    expect(jrRows[0]!.children[4]!.textContent).toBe('—')
    expect(jrRows[1]!.children[4]!.textContent).toBe('合成管理员')
    expect(jrRows[2]!.children[4]!.textContent).toBe('自动')
    // remote 块原样展示（本夹具给了 remote）。
    expect(body.textContent).toContain('远端返回（原样）')

    body.querySelector<HTMLElement>('.qqbotJrApprove')!.click()
    await flush()
    const approveCall = callTo('post', '/api/qqbot/groups/5/join-requests/approve/')
    expect(approveCall.data).toEqual({
      op: 'approve',
      join_request_id: 'synthetic-jr-1',
      member_openid: 'synthetic-member-1',
    })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.joinApproved)

    // 审批失败分支：ok=false ⇒ 把远端那句说出来（留痕行后端照样写）。
    ready('POST /api/qqbot/groups/5/join-requests/approve/', () => ({
      ok: false,
      decision: 'failed',
      error: { code: '11244', message: '申请已过期' },
    }))
    body.querySelector<HTMLElement>('.qqbotJrDecline')!.click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('申请已过期')

    // 审批策略：切 whitelist、填名单 → PATCH 载荷带解析后的数组。
    const select = body.querySelector<HTMLSelectElement>('.qqbotPolicySelect')!
    select.value = 'whitelist'
    select.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    const input = card.querySelector<HTMLTextAreaElement>(`.${WHITELIST_INPUT_CLASS}`)!
    expect(input.disabled).toBe(false)
    input.value = 'synthetic-member-1, synthetic-member-2\n\nsynthetic-member-1'
    card.querySelector<HTMLElement>('.qqbotPolicySave')!.click()
    await flush()
    expect(callTo('patch', '/api/qqbot/groups/5/join-policy/').data).toEqual({
      mode: 'whitelist',
      whitelist: ['synthetic-member-1', 'synthetic-member-2'],
    })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.policySaved)
  })

  it('⭐ 评审 P1-2：刷新后回填已存名单——群行带回的名单进编辑区（修复前刷新即失）', async () => {
    // 群 B 是白名单模式、名单在**群行**里（P1-2 起后端读面带回）⇒ 首次展开就能看到，
    // ⛔ 不需要先 PATCH（修复前 savedWhitelists 只在 PATCH 响应后有值，刷新即空）。
    ready('GET /api/qqbot/groups/6/join-requests/', () => ({
      items: [],
      remote: { items: [], total: 0 },
    }))
    await mount()
    ;(groupCard(6).querySelector<HTMLElement>(`.${JR_TOGGLE_CLASS}`)!).click()
    await flush()
    const input = groupCard(6).querySelector<HTMLTextAreaElement>(
      `.${WHITELIST_INPUT_CLASS}`,
    )!
    // whitelist 模式 ⇒ 编辑区是放开的（⛔ vitest 的 toBe 不收第二个消息参数，说明写注释）。
    expect(input.disabled).toBe(false)
    expect(input.value).toBe('synthetic-member-kept')

    // 服务端那份名单被外部改过（另一个管理员改的）⇒ 重拉群清单后**跟到新值**
    // （loadGroups 用群行覆盖内存那份）。renderGroups 重画卡片会把展开区收起 ⇒ 重新展开再取。
    groupsOnServer = groupsOnServer.map((g) =>
      g.id === 6 ? { ...g, join_policy_whitelist: ['synthetic-member-new'] } : g,
    )
    ;(groupCard(5).querySelector<HTMLElement>(`.${REFRESH_INFO_CLASS}`)!).click()
    await flush()
    ;(groupCard(6).querySelector<HTMLElement>(`.${JR_TOGGLE_CLASS}`)!).click()
    await flush()
    const inputAfter = groupCard(6).querySelector<HTMLTextAreaElement>(
      `.${WHITELIST_INPUT_CLASS}`,
    )!
    expect(inputAfter.value).toBe('synthetic-member-new')
  })

  // ── 分享链接：生成 + 复制 ──────────────────────────────────────────────────

  it('分享链接：选群 → 生成 → 链接上屏可复制；没选群先提示', async () => {
    await mount()
    at<HTMLButtonElement>(SHARE_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.menuPickGroup)

    const writeText = stubClipboard()
    at<HTMLSelectElement>(SHARE_GROUP_SELECT_ID).value = '5'
    at<HTMLButtonElement>(SHARE_BUTTON_ID).click()
    await flush()
    expect(callTo('post', '/api/qqbot/share-link/').data).toEqual({ group: 5 })
    expect(at(SHARE_RESULT_ID).textContent).toContain('https://synthetic.qq/share/link')
    expect(at<HTMLButtonElement>(SHARE_COPY_BUTTON_ID).style.display).not.toBe('none')
    at<HTMLButtonElement>(SHARE_COPY_BUTTON_ID).click()
    await flush()
    expect(writeText.mock.calls[0]![0]).toBe('https://synthetic.qq/share/link')
  })

  // ── ⭐ 页内自检那道门（深链）与节点码 ─────────────────────────────────────

  it('⭐ 非账号管理员深链打开 ⇒ 提示无权 + **不注入标记** + 一个请求都不发（fail-closed）', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 2,
      username: 'synthetic-kefu',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      // 就算**持有**那个节点也不行：路由那第二道门是账号管理员。
      menu_permissions: ['system.qqbot_settings'],
      permissions: [],
    })
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    expect(root.innerHTML).toBe('')
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('页内自检用的节点码与 `config/routePermissions.ts` 那条**同值**（字面值钉，E-0024）', async () => {
    // ⚠️ 引的是 api 模块导出的**字面值比较**，不是"两边都引同一个常量"那种自指断言。
    const mod = (await import('@/api/qqbot')) as Record<string, unknown>
    expect(mod.QQBOT_SETTINGS_NODE).toBe('system.qqbot_settings')
    // 对照（防"模块没加载"造成的恒真）：同模块的另几个导出确实在。
    expect(Object.keys(mod)).toContain('getQQBotBinding')
    expect(Object.keys(mod)).toContain('listQQBotMessages')
    // 运行时形状：全部端点都在 `/api/qqbot/` 下，⛔ 没有别的前缀。
    for (const url of [
      mod.QQBOT_BINDING_URL as string,
      mod.QQBOT_GROUPS_URL as string,
      mod.QQBOT_MENU_URL as string,
      mod.QQBOT_SHARE_LINK_URL as string,
      mod.QQBOT_MESSAGES_URL as string,
    ]) {
      expect(url.startsWith('/api/qqbot/')).toBe(true)
    }
  })

  // ── 纯函数（占位 / 白名单解析 / 菜单载荷）──────────────────────────────────

  it('`orWhitelistPending` / `memberNumText`：空串与 null 是占位，0 是合法值（E-0021）', () => {
    expect(__testables.orWhitelistPending('')).toBe('白名单未批')
    expect(__testables.orWhitelistPending('合成群')).toBe('合成群')
    expect(__testables.memberNumText(null)).toBe('白名单未批')
    expect(__testables.memberNumText(0)).toBe('0')
  })

  it('`parseWhitelistInput`：换行 / 逗号（含全角）分隔、去空白、去重', () => {
    expect(__testables.parseWhitelistInput('a\nb，c')).toEqual(['a', 'b', 'c'])
    expect(__testables.parseWhitelistInput(' a ,\n a \n b ')).toEqual(['a', 'b'])
    expect(__testables.parseWhitelistInput('')).toEqual([])
  })

  it('`buildMenuButtons` / `parseMenuButtons`：与后端测试里那份合法载荷同形（往返一致）', () => {
    const rows = [
      { name: '官网', type: 'link' as const, link: 'https://x.example', subs: [] },
      {
        name: '服务',
        type: 'menu' as const,
        link: '',
        subs: [{ name: '联系客服', type: 'send_message' as const, link: '' }],
      },
    ]
    expect(__testables.buildMenuButtons(rows)).toEqual([
      { name: '官网', type: 'link', link: 'https://x.example' },
      { name: '服务', sub_button: [{ name: '联系客服', type: 'send_message' }] },
    ])
    // 反向：官方回包 → 编辑器状态 → 载荷，与原回包同形。
    const official = {
      buttons: [
        { name: '官网', type: 'link', link: 'https://x.example' },
        { name: '服务', sub_button: [{ name: '联系客服', type: 'send_message' }] },
      ],
    }
    expect(__testables.buildMenuButtons(__testables.parseMenuButtons(official)!)).toEqual(
      official.buttons,
    )
    // 不认识的形状如实回 null（页面会说"解析不了"，⛔ 不硬造一行）。
    expect(__testables.parseMenuButtons({ nope: 1 })).toBeNull()
    expect(__testables.parseMenuButtons('x')).toBeNull()
  })

  it('`menuPayloadErrors`：行数 / 名称长度 / 子菜单上限 / https 逐条给出中文', () => {
    expect(__testables.menuPayloadErrors([])).toEqual([])
    const linkRow = { name: '官网', type: 'link' as const, link: 'http://x', subs: [] }
    expect(__testables.menuPayloadErrors([linkRow])).toEqual([
      '第 1 个一级菜单：link 类型菜单的链接必须以 https:// 开头',
    ])
    const longName = { name: '一二三四五六七八九十十一', type: 'switch' as const, link: '', subs: [] }
    expect(__testables.menuPayloadErrors([longName])).toEqual([
      '第 1 个一级菜单名称过长（最多 10 个字符）',
    ])
    const manySubs = {
      name: '服务',
      type: 'menu' as const,
      link: '',
      subs: Array.from({ length: 6 }, () => ({ name: '子', type: 'send_message' as const, link: '' })),
    }
    const errors = __testables.menuPayloadErrors([manySubs])
    expect(errors).toEqual(['第 1 个一级菜单的二级菜单最多 5 个'])
  })
})
