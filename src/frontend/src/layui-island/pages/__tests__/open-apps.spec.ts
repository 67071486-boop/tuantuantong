// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import type { ApiAppRow, ApiDeliveryRow } from '@/api/openApps'

/**
 * 【开放平台 → 应用管理】island 页的契约测试。
 *
 * 这一页是**自有功能**（对标物里没有），所以没有证据件可对拍；本文件钉的是**有真实后果**的约定：
 *
 * 1. **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    **新建 → 拿到密钥 → 打开「回调与推送」→ 改地址 + 关开关 → 保存 → 列表刷新** 一整条链，
 *    外加推送记录的展开 / 重投、停用启用、轮换密钥、**复制密钥**、以及一条降级分支。
 * 2. **四条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量、以 `(` 开头的语句前置分号、
 *    先挂样式 → 再塞标记 → 最后 `initIslandElements`（以**运行期 DOM** 判断，不是读源码）、
 *    页里有定时器时假定时器要先于 `setInterval` 包装且用例自己收口
 *    （⚠️ 本页**没有**定时器，所以这一条在这里体现为「各用例自己 `document.body.innerHTML = ''` 收口」）。
 * 3. ⭐ **「应用 = 全租户」的代价在页面上说破**（设计文档 §4-V2 / §8 / §14-11）：
 *    列表上方那段 quote 里必须有「本应用可访问本租户全部业务数据」。
 * 4. ⛔ **没有删除**（业务方选定「只能停用不能删」）：页面上没有删除入口、api 模块里
 *    连 `delete` 方法都没有。
 * 5. ⭐ **列表里就显示密钥**（业务方口径：明文展示、随时可取）：
 *    - 列表数据的 `app_secret` 等于服务端给的值，且**渲染进了那一格**（走 `escapeHtml`）；
 *    - 「操作」那格有**复制密钥**动作，复制的是 `AppKey + AppSecret` 两行；
 *    - ⛔ **页面的标记里没有密钥**（密钥不进 DOM 属性）；
 *    - ⛔ 页面上**不再有**「只显示这一次 / 关了就没」那类措辞。
 * 6. ⭐ **回调与推送**（设计文档 §7.1 / §7.4 / §13.18-3）：
 *    - 「回调地址」+「回调开关」一次 PATCH 两个键（`callback_url` / `callback_state`）；
 *    - 地址非 https ⇒ **当场给一句提示**；但**判据以后端为准**（后端回 `1000` 时那句中文要显示出来，
 *      ⛔ 前端不把它挡在门外）；
 *    - 「最近推送记录」是**可折叠**面板，列出时间 / 事件 / 订单号 / 状态 / 重试次数 / 失败原因；
 *    - ⭐ **重投按钮只出现在 `failed` 行上**（后端对 `pending` / `done` 回 `1000`），
 *      点了要二次确认 → `POST .../deliveries/<id>/retry/` → **重新拉一次台账**；
 *    - 台账取不到时**如实显示**后端文案，⛔ 不渲染成"还没有推送记录"。
 * 7. ⛔ **权限设置 / 游戏范围两块已按业务方要求删除** ⇒ 判据打在**标记**与
 *    **运行时导出**上（⛔ 不是"源码文件文本里没有这个词"，`AGENTS.md` §5-G / E-0161）：
 *    「操作」那格恰好四个 `lay-event`（`callback` / `copysecret` / `toggle` / `rotate`）、
 *    整页可见文字里没有「权限设置」/「游戏范围」、`@/api/openApps` 也不再导出那三个替换函数。
 *
 * ## 手法（照 `system-perfanalysis.spec.ts`）
 * ⚠️ `thirdparty-jidanpu-sync.spec.ts` 那个先例文件已删除（⛔ 别去找它）——本文件这一套照旧生效。
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `table.render` /
 *   `table.reloadData` / `layer.*` 的**入参**才能被断言；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/openApps` 跑的是**真实现** ⇒
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义（把 api 模块也换成替身的话，
 *   那只是在断言自己传了一个对象）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：应用名 / 游戏名 / 密钥 / 单号都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const renders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const opens: Array<Record<string, unknown>> = []
  const confirms: string[] = []
  const confirmOks: Array<() => void> = []
  const closeAlls: string[] = []
  const handlers: Record<string, (obj: unknown) => void> = {}
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    renders,
    reloads,
    opens,
    confirms,
    confirmOks,
    closeAlls,
    handlers,
    seenByBootstrap,
    seenByInit,
    table: {
      render: vi.fn((o: Record<string, unknown>) => {
        renders.push(o)
      }),
      reloadData: vi.fn((id: string, o: { data: unknown }) => {
        reloads.push({ id, data: o.data })
      }),
      on: vi.fn((event: string, cb: (obj: unknown) => void) => {
        handlers[event] = cb
      }),
    },
    layer: {
      open: vi.fn((o: Record<string, unknown>) => {
        opens.push(o)
        return 1
      }),
      closeAll: vi.fn((t?: string) => {
        closeAlls.push(t ?? '')
      }),
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
      confirm: vi.fn((text: string, _o: unknown, ok: () => void) => {
        confirms.push(text)
        confirmOks.push(ok)
      }),
    },
    // `layer.open({content: $(panel)})` 用的那个 `$`：返回一个不抛的替身
    // （本页不依赖它的返回值形状）。
    $: vi.fn((el: HTMLElement) => ({ el, slideUp: vi.fn(), slideDown: vi.fn() })),
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#openAppsCallbackSaveBtn') !== null)
    return { table: LAYUI.table, layer: LAYUI.layer, $: LAYUI.$ }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#openAppsCallbackSaveBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

/** 身份：本页第一句就 `fetchMe()`（自检那道门）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  CALLBACK_APP_NAME_ID,
  CALLBACK_FORM_FILTER,
  CALLBACK_HINT_ID,
  CALLBACK_NOTICE,
  CALLBACK_SAVE_BUTTON_ID,
  CALLBACK_STATE_NAME,
  CALLBACK_STATE_SWITCH_ID,
  CALLBACK_URL_HINT,
  CALLBACK_URL_INPUT_ID,
  COST_NOTICE,
  DELIVERIES_BODY_ID,
  DELIVERIES_TOGGLE_CLOSED,
  DELIVERIES_TOGGLE_ID,
  DELIVERIES_TOGGLE_OPEN,
  DELIVERIES_WARN_ID,
  DELIVERY_RETRY_ATTR,
  DOC_ACTIVE_CLASS,
  DOC_ACTIVE_SELECTOR,
  DOC_ANCHOR_ATTR,
  DOC_BODY_ID,
  DOC_COPY_BUTTON_ID,
  DOC_DOWNLOAD_BUTTON_ID,
  DOC_DOWNLOAD_FILENAME,
  DOC_PANEL_TITLE,
  DOC_SELECTOR,
  DOC_TOC_ID,
  DOC_TOC_ITEM_SELECTOR,
  DOC_TOC_LIST_SELECTOR,
  DOC_TOOLBAR_EVENT,
  FORM_SELECTOR,
  FULL_TENANT_NOTICE,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  PII_NOTICE,
  SECRET_PANEL_NOTICE,
  SECRET_TEXT_ID,
  TABLE_ID,
  __testables,
} from '../open-apps'

const {
  actionCell,
  apiDoc,
  buildHtml,
  callbackSaveReport,
  callbackUrlHint,
  copyCredentialText,
  decorateRows,
  deliveriesHtml,
  docTocHtml,
  downloadTextFile,
  htmlToText,
  lastUsedText,
  renderApiDoc,
  secretCell,
  stateActionLabel,
  timeText,
} = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; data?: unknown }

/**
 * 响应路由：按 `METHOD URL` 认领。**没有备好响应就抛**（⛔ 不是静默回 undefined ——
 * 那会让"页面根本没发这条请求"与"发了但没人管"长得一样）。
 */
let routes: Record<string, (data?: unknown) => unknown>

function ready(key: string, fn: (data?: unknown) => unknown): void {
  routes[key] = fn
}

// ── 夹具（**全部合成**）────────────────────────────────────────────────────

/**
 * 应用甲：启用了回调（合法 https 地址）。
 * ⚠️ 行载荷里**没有** `permissions` / `games` / `all_games` / `permission_codes` /
 * `game_ids` 那五个键（权限与游戏范围两块随后端一起废除），多了 `callback_url` / `callback_state`。
 */
const APP_A: ApiAppRow = {
  id: 1,
  name: '应用甲',
  app_key: '0123456789abcdef',
  // ⭐ 改口径：`app_secret` 现在**就在行载荷里**（列表也回它）⇒ 夹具必须有。
  app_secret: 'SYNTHETIC-secret-A1b2C3d4',
  state: 'enabled',
  state_text: '启用',
  ip_allowlist: '',
  callback_url: 'https://callback.example.com/tt',
  callback_state: 'enabled',
  last_used_at: null,
  secret_rotated_at: null,
  created_at: '2026-10-07T10:00:00+08:00',
}

/** 应用乙：停用、**没配回调**（地址空 + 开关停用 ⇒ 不会推送）。 */
const APP_B: ApiAppRow = {
  id: 2,
  name: '应用乙',
  app_key: 'fedcba9876543210',
  app_secret: 'SYNTHETIC-secret-B5e6F7g8',
  state: 'disabled',
  state_text: '停用',
  ip_allowlist: '10.0.0.0/8',
  callback_url: '',
  callback_state: 'disabled',
  last_used_at: '2026-10-07T09:00:00+08:00',
  secret_rotated_at: '2026-10-07T08:00:00+08:00',
  created_at: '2026-10-06T10:00:00+08:00',
}

/**
 * 应用己：**旧后端**的形态（行载荷里**没有**回调那两个键）⇒ 走**大声降级**那一支
 * （文件头那条：⛔ 不许当成"没配回调"显示成空 —— 那会让用户一保存就覆盖掉真实配置）。
 */
const APP_NO_CALLBACK: ApiAppRow = (() => {
  const row: Record<string, unknown> = { ...APP_A, id: 5, name: '应用己' }
  delete row.callback_url
  delete row.callback_state
  return row as unknown as ApiAppRow
})()

/** 创建那一次拿到的应用（**改口径之后**它就是普通的一行：密钥本来就在行载荷里）。 */
const CREATED: ApiAppRow = {
  ...APP_A,
  id: 9,
  name: '应用丁',
  app_secret: 'SYNTHETIC-secret-CREATED-Q1w2E3',
  callback_url: '',
  callback_state: 'disabled',
}

const ROTATED: ApiAppRow = { ...APP_A, app_secret: 'SYNTHETIC-secret-ROTATED-Z9y8X7' }

/**
 * 台账三行（**三种状态各一条**，判据要能分辨它们）：
 * `31` = 失败（**唯一**该有「重投」按钮的行）、`32` = 已投递、`33` = 待投递。
 */
const DELIVERY_FAILED: ApiDeliveryRow = {
  id: 31,
  event: 'order.status_changed',
  event_text: '订单状态变化',
  order_no: 'TT202610080001',
  source_order_no: '54556512310200',
  status: 'failed',
  status_text: '投递失败',
  attempts: 7,
  reason: '对方回执不是 2xx',
  response_status: 500,
  next_attempt_at: null,
  created_at: '2026-10-08T17:20:00+08:00',
  processed_at: '2026-10-08T17:30:00+08:00',
}

const DELIVERY_DONE: ApiDeliveryRow = {
  id: 32,
  event: 'order.image_added',
  event_text: '订单新增进度图',
  order_no: 'TT202610080002',
  source_order_no: '',
  status: 'done',
  status_text: '已投递',
  attempts: 1,
  reason: '',
  response_status: 200,
  next_attempt_at: null,
  created_at: '2026-10-08T17:25:00+08:00',
  processed_at: '2026-10-08T17:25:10+08:00',
}

const DELIVERY_PENDING: ApiDeliveryRow = {
  id: 33,
  event: 'order.deleted',
  event_text: '订单删除',
  order_no: 'TT202610080003',
  source_order_no: '',
  status: 'pending',
  status_text: '待投递',
  attempts: 1,
  reason: '连接超时',
  response_status: null,
  next_attempt_at: '2026-10-08T17:31:00+08:00',
  created_at: '2026-10-08T17:30:00+08:00',
  processed_at: '2026-10-08T17:30:05+08:00',
}

const DELIVERY_PAGE = {
  items: [DELIVERY_FAILED, DELIVERY_DONE, DELIVERY_PENDING],
  total: 3,
  page: 1,
  page_size: 20,
}

function flush(): Promise<void> {
  // 页面的取数链是**多跳**的（点击 → request → 再 request → 上屏）：连放几轮宏任务。
  return (async () => {
    for (let i = 0; i < 6; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

function rowTool(row: ApiAppRow, event: string): void {
  LAYUI.handlers[`tool(${TABLE_ID})`]!({ data: row, event })
}

function toolbar(event: string): void {
  LAYUI.handlers[`toolbar(${TABLE_ID})`]!({ event })
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
function callCount(url: string): number {
  return mockedRequest.mock.calls.map((c) => c[0] as Cfg).filter((c) => c.url === url).length
}

/** 表里最后一次灌进去的数据（= 页面渲染的全部行）。 */
function tableRows(): ApiAppRow[] {
  const last = LAYUI.reloads[LAYUI.reloads.length - 1]
  if (!last) throw new Error('没有对列表调用过 reloadData')
  return last.data as ApiAppRow[]
}

describe('应用管理 island 页（open-apps）', () => {
  let root: HTMLElement
  /** 服务端**此刻**那份列表（创建 / PATCH / 轮换会改它；见 `beforeEach` 里"活的响应"那段）。 */
  let appsOnServer: ApiAppRow[]
  /** 服务端**此刻**那份台账（重投会改它 ⇒ 刷新之后能看出变化）。 */
  let deliveriesOnServer: typeof DELIVERY_PAGE

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
    LAYUI.renders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.opens.length = 0
    LAYUI.confirms.length = 0
    LAYUI.confirmOks.length = 0
    LAYUI.closeAlls.length = 0
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    for (const key of Object.keys(LAYUI.handlers)) delete LAYUI.handlers[key]
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
      menu_permissions: ['system.pinduoduo_settings'],
      permissions: [],
    })

    routes = {}
    // ⚠️ **列表的响应是"活的"**：创建 / PATCH / 轮换之后重新取列表必须**看得到新值**
    //    （否则"列表里就有密钥"这条判据只是在断言**夹具**，而不是页面的行为）。
    //    替换条件钉在**字段值**上（⛔ 不是"调用了几次"，那会随无关改动漂）。
    appsOnServer = [APP_A, APP_B]
    deliveriesOnServer = DELIVERY_PAGE
    ready('GET /api/open/apps/', () => appsOnServer)
    ready('POST /api/open/apps/', () => {
      appsOnServer = [...appsOnServer, CREATED]
      return CREATED
    })
    // PATCH 把入参**并回**这一行（PATCH 语义：缺席的键保持库里的值）⇒ 刷新后看得到改动。
    ready('PATCH /api/open/apps/1/', (data) => {
      const patch = (data ?? {}) as Partial<ApiAppRow>
      appsOnServer = appsOnServer.map((row) => (row.id === 1 ? { ...row, ...patch } : row))
      return appsOnServer.find((row) => row.id === 1)!
    })
    ready('PATCH /api/open/apps/2/', () => ({ ...APP_B, state: 'enabled', state_text: '启用' }))
    ready('POST /api/open/apps/1/rotate-secret/', () => {
      appsOnServer = appsOnServer.map((row) => (row.id === ROTATED.id ? ROTATED : row))
      return ROTATED
    })
    ready('GET /api/open/apps/1/deliveries/', () => deliveriesOnServer)
    // 重投：把 `31` 排回待投递（照后端 §13.18-3 的口径：`status=pending` / `attempts=0` /
    // `next_attempt_at=None`，`reason` 与 `response_status` 保留供对照）。
    ready('POST /api/open/apps/1/deliveries/31/retry/', () => {
      deliveriesOnServer = {
        ...deliveriesOnServer,
        items: deliveriesOnServer.items.map((d) =>
          d.id === 31
            ? {
                ...d,
                status: 'pending' as const,
                status_text: '待投递',
                attempts: 0,
                next_attempt_at: null,
              }
            : d,
        ),
      }
      return deliveriesOnServer.items.find((d) => d.id === 31)!
    })
    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url ?? ''}`
        const fn = routes[key]
        if (!fn) throw new Error(`测试没有为 ${key} 准备响应`)
        return fn(cfg.data)
      }),
    )
  })

  afterEach(() => {
    // 每个用例自己收口（`AGENTS.md` §5-G 陷阱 ④：上一个用例挂载的页面会继续打**同一个**替身）。
    document.body.innerHTML = ''
  })

  async function mount(): Promise<void> {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  }

  /** 打开「回调与推送」面板（行操作 `callback`）。 */
  async function openCallbackPanel(row: ApiAppRow): Promise<void> {
    rowTool(row, 'callback')
    await flush()
  }

  /** 面板里的某个节点（弹层借走后仍在文档里 ⇒ 从 root 查得到）。 */
  function at<T extends HTMLElement>(id: string): T {
    const el = root.querySelector<T>(`#${id}`)
    if (!el) throw new Error(`页面上没有 #${id}`)
    return el
  }

  // ── 挂载顺序（陷阱 ③，运行期判据）────────────────────────────

  it('先挂样式再塞标记：bootstrapIsland 看不到标记、initIslandElements 看得到', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('类名与选择器是两个常量：标记里是 `class="openAppsForm"`（⛔ 不是 `.openAppsForm`）', async () => {
    await mount()
    expect(root.querySelector('.openAppsForm')).not.toBeNull()
    // 写成 `class="${PANEL_SELECTOR}"` 会造出 `class=".openAppsForm"`、永远匹配不到。
    // ⚠️ 用**标记原文**判（`..openAppsForm` 是非法选择器，jsdom 直接抛）。
    expect(root.innerHTML).not.toContain('class=".openAppsForm"')
    expect(root.querySelector(FORM_SELECTOR)).not.toBeNull()
    // 弹层里要用的容器都在（少一个就是"点了没反应"）。
    expect(root.querySelector('.openAppsSecret')).not.toBeNull()
    expect(root.querySelector('.openAppsCallback')).not.toBeNull()
    expect(root.querySelector(`#${CALLBACK_URL_INPUT_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${CALLBACK_STATE_SWITCH_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${DELIVERIES_TOGGLE_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${DELIVERIES_BODY_ID}`)).not.toBeNull()
  })

  // ── 列表 ────────────────────────────────────────────────────

  it('挂载时取一次列表，行**原样**灌进表（只多一个本页预算的文案字段）；密钥**就在行里**', async () => {
    await mount()
    expect(callTo('get', '/api/open/apps/')).toBeTruthy()
    // 服务端那几个字段**原样**上屏：本页只**加**一个字段（`state_action_label`），
    // 所以这里先把它摘掉再逐字段比对；那个字段的值由下面的「状态按钮」用例逐字钉死。
    const strip = (row: Record<string, unknown>): Record<string, unknown> => {
      const copy = { ...row }
      delete copy.state_action_label
      return copy
    }
    expect((tableRows() as Array<Record<string, unknown>>).map(strip)).toEqual([APP_A, APP_B])
    expect(
      (tableRows() as Array<ApiAppRow & { state_action_label?: string }>).map(
        (r) => r.state_action_label,
      ),
    ).toEqual(['停用', '启用'])
    // ⭐ **改口径**：列表数据里**有**密钥、且**各是各的**（原判据是"不许有"）。
    expect(tableRows().map((r) => r.app_secret)).toEqual([
      'SYNTHETIC-secret-A1b2C3d4',
      'SYNTHETIC-secret-B5e6F7g8',
    ])
    const titles = (
      LAYUI.renders[0]!.cols as Array<Array<{ title?: string }>>
    )[0]!.map((c) => c.title)
    // ⭐ 列表**没有**「权限」「游戏范围」两列（业务方要求删掉那两块）。
    expect(titles).toEqual(['应用名', 'AppKey', 'AppSecret', '状态', '最近使用', '操作'])
  })

  it('⭐ AppSecret 那一格：明文上屏、走转义、且**标记里没有密钥**', async () => {
    await mount()
    const cols = (
      LAYUI.renders[0]!.cols as Array<Array<{ title?: string; templet?: (r: ApiAppRow) => string }>>
    )[0]!
    const secretCol = cols.find((c) => c.title === 'AppSecret')!
    // ① 明文（业务方要的就是"看得见"）：值逐字上屏。
    expect(secretCol.templet!(APP_A)).toContain('SYNTHETIC-secret-A1b2C3d4')
    // ② 长串要能折行（被裁掉就成了"半句密钥"，复制出去是错的）。
    expect(secretCol.templet!(APP_A)).toContain('word-break:break-all')
    // ③ 走转义（⛔ 不假设密钥的字符集：它来自库，而库里的值可能被人改）。
    const evil: ApiAppRow = { ...APP_A, app_secret: '"><img src=x onerror=alert(1)>' }
    expect(secretCell(evil)).not.toContain('<img')
    expect(secretCell(evil)).toContain('&lt;img')
    // ④ ⛔ **「操作」那一格的标记里没有密钥**：密钥不进 `data-*` / `value=` / `id=` 等 DOM 属性
    //    （那样截图、调试面板、页面快照都会多出副本）⇒ 复制动作不会把密钥写进标记。
    //    ⚠️ 密钥**当然**出现在 `secretCell` 的**文本**里（那就是"明文展示"本身），
    //    所以这条判据只打在**操作格**上——⛔ 别把它写成"整页 HTML 里没有密钥"（那是错的判据：
    //    它会把业务方要的功能判成缺陷）。
    const cells = tableRows().map((r) => actionCell(decorateRows([r])[0]!))
    for (const cell of cells) {
      expect(cell).not.toContain('SYNTHETIC-secret')
      expect(cell).not.toContain('data-clipboard-text')
      expect(cell).not.toContain(APP_A.app_secret)
      expect(cell).not.toContain(APP_B.app_secret)
    }
    // 对照：操作格**确实**渲染了那四颗按钮（否则上面那三条会因为"格子是空的"而恒真）。
    expect(cells[0]).toContain('lay-event="copysecret"')
  })

  it('⭐ 「复制密钥」：复制的是 AppKey + AppSecret 两行（从行数据取，⛔ 不从 DOM）', async () => {
    await mount()
    const writeText = vi.fn((_text: string) => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    rowTool(APP_A, 'copysecret')
    await flush()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0]![0]).toBe(copyCredentialText(APP_A))
    expect(writeText.mock.calls[0]![0]).toContain(APP_A.app_key)
    expect(writeText.mock.calls[0]![0]).toContain(APP_A.app_secret)
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(
      `已复制「${APP_A.name}」的 AppKey 与 AppSecret`,
    )
  })

  it('⭐ 复制失败（浏览器不给剪贴板）**说出来**，⛔ 不静默假装成功', async () => {
    await mount()
    // 先证"接上了"：jsdom 里 `navigator.clipboard` 默认**不存在** ⇒ 走失败那一支。
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    rowTool(APP_A, 'copysecret')
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.copyFailed)
    // 换个"存在但被拒"的替身 ⇒ 同样要报失败（⛔ 不是只在"没有 clipboard"时才报）。
    LAYUI.messages.length = 0
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: () => Promise.reject(new Error('NotAllowedError')) },
      configurable: true,
    })
    rowTool(APP_A, 'copysecret')
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.copyFailed)
  })

  // ── ⭐ 「应用 = 全租户」的代价说破（§4-V2 / §8 / §14-11）────────

  it('⭐ 列表上方显式告知「本应用可访问本租户全部业务数据」（⛔ 不是隐性默认）', async () => {
    await mount()
    // 判据打在**列表页自己身上**那段 quote 上（⛔ 不是某个必须点开才看得见的弹层里）。
    const quote = Array.from(root.querySelectorAll('blockquote')).find(
      (el) => el.closest('.openAppsCallback') === null,
    )!
    expect(quote.textContent).toContain('本应用可访问本租户全部业务数据')
    expect(quote.textContent).toContain(FULL_TENANT_NOTICE)
    // 成本 / PII 两句也在这段 quote 里（它们说的是"第三方能看到什么"，与权限块无关）。
    expect(quote.textContent).toContain(COST_NOTICE)
    expect(quote.textContent).toContain(PII_NOTICE)
    // 对照：这一段是**挂载即可见**的（没有任何弹层参与）——否则上面三条只是因为"别处也有这句话"。
    expect(LAYUI.opens).toHaveLength(0)
  })

  // ── ⛔ 权限 / 游戏范围两块已删（判据打在标记与运行时导出上）──────

  it('⛔ 标记上不再有权限 / 游戏范围入口（判据打在**标记**与**运行时导出**上）', async () => {
    await mount()
    // ① 「操作」那一格（函数 templet 的输出，⛔ 不含注释）：恰好四个 lay-event，且没有 perm。
    const cell = actionCell(decorateRows([APP_A])[0]!)
    expect(cell).not.toContain('lay-event="perm"')
    expect(cell.match(/lay-event="([a-z]+)"/g)).toEqual([
      'lay-event="callback"',
      'lay-event="copysecret"',
      'lay-event="toggle"',
      'lay-event="rotate"',
    ])
    // ② 整页**可见文字**里没有那两块（用渲染后的 DOM 纯文本判：注释不进 textContent，
    //    ⛔ 这正是 E-0161 要求的口径——不许拿"源码文件文本"当判据）。
    const text = root.textContent ?? ''
    expect(text).not.toContain('权限设置')
    expect(text).not.toContain('游戏范围')
    expect(text).not.toContain('已配置')
    expect(text).not.toContain('全部游戏')
    // ③ 标记原文（`buildHtml` 的输出，本身不含注释）里也没有那两个事件与那三组 id。
    const html = buildHtml()
    expect(html).not.toContain('lay-event="perm"')
    expect(html).not.toContain('openAppsPermSaveBtn')
    expect(html).not.toContain('openAppsGamesSaveBtn')
    expect(html).not.toContain('openAppsAllGames')
    expect(html).not.toContain('chengeRole')
    // ④ 运行时导出：调用方**拿不到**那三个替换函数、两条旧 URL 与那张节点码表
    //    （⛔ 不是读源码文本判断 —— 这里问的是"模块到底导出了什么"）。
    const mod = (await import('@/api/openApps')) as Record<string, unknown>
    for (const gone of [
      'replaceAppPermissions',
      'replaceAppGames',
      'replaceAppAllGames',
      'appPermissionsUrl',
      'appGamesUrl',
      'OPEN_ENDPOINT_NODE_CODES',
    ]) {
      expect(Object.keys(mod)).not.toContain(gone)
    }
    // 对照：同一批断言里**活着**的导出确实在（否则上面那一条会因为"模块没加载"而恒真）。
    expect(Object.keys(mod)).toContain('listApiApps')
    expect(Object.keys(mod)).toContain('listDeliveries')
    expect(Object.keys(mod)).toContain('retryDelivery')
    // ⑤ **运行时形状**（⛔ 不是读源码文本判断）：应用管理下今天那四条 URL 里
    //    **一条 `permissions` / `games` 路径都不存在**（那两条写端点随后端一起废除）。
    const urls = [
      (mod.appDetailUrl as (pk: number) => string)(7),
      (mod.appRotateSecretUrl as (pk: number) => string)(7),
      (mod.appDeliveriesUrl as (pk: number) => string)(7),
      (mod.appDeliveryRetryUrl as (pk: number, d: number) => string)(7, 9),
    ]
    for (const url of urls) {
      expect(url.startsWith('/api/open/apps/7/')).toBe(true)
      expect(url).not.toContain('permissions')
      expect(url).not.toContain('games')
    }
  })

  // ── ⭐ 一整条链：新建 → 拿到密钥 → 回调设置 → 保存 → 列表刷新 ──

  it('新建 → 拿到密钥 → 打开回调与推送 → 改地址 + 关开关 → 保存 → 列表刷新（**一条链**）', async () => {
    await mount()
    expect(LAYUI.reloads).toHaveLength(1)

    // ① 工具栏「新增应用」→ 弹层打开（内容就是那个表单面板）。
    toolbar('add')
    expect(LAYUI.opens).toHaveLength(1)
    expect(LAYUI.opens[0]).toMatchObject({ type: 1, title: '新增开放平台应用' })

    // ② 填名字 + 点创建 ⇒ 真发 POST，body 里**只有** name（密钥不是入参）。
    root.querySelector<HTMLInputElement>('#openAppsName')!.value = '  应用丁  '
    root.querySelector<HTMLButtonElement>('#openAppsCreateSaveBtn')!.click()
    await flush()
    expect(callTo('post', '/api/open/apps/').data).toEqual({ name: '应用丁' })

    // ③ 建完弹一层把密钥摆出来（方便当场复制）；⚠️ **不是"唯一能看到的地方"**
    //    ⇒ 这一层的文案里⛔ 不许有「只显示这一次 / 关了就没」（那是与实现相反的话）。
    expect(LAYUI.opens).toHaveLength(2)
    expect(LAYUI.opens[1]!.title).not.toContain('只显示')
    expect(root.innerHTML).toContain(SECRET_PANEL_NOTICE)
    expect(root.innerHTML).not.toContain('只显示这一次')
    expect(root.innerHTML).not.toContain('再也看不到')
    expect(root.querySelector<HTMLTextAreaElement>(`#${SECRET_TEXT_ID}`)!.value).toBe(
      CREATED.app_secret,
    )
    // ④ 列表已刷新，而且**列表数据里就有**这把密钥（"随时可以获取"的页面侧判据）。
    expect(LAYUI.reloads).toHaveLength(2)
    expect(tableRows().map((r) => r.app_secret)).toContain(CREATED.app_secret)

    // ⑤ 打开「回调与推送」：额外取一次推送台账。
    mockedRequest.mockClear()
    await openCallbackPanel(APP_A)
    expect(callTo('get', '/api/open/apps/1/deliveries/')).toBeTruthy()
    expect(LAYUI.opens).toHaveLength(3)
    expect(LAYUI.opens[2]!.title).toContain('回调与推送：应用甲')
    expect(at(CALLBACK_APP_NAME_ID).textContent).toBe('应用甲')

    // ⑥ 回填 = **这一行自己的**配置（⛔ 不是空、也不是别的应用的）。
    expect(at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value).toBe(APP_A.callback_url)
    expect(at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID).checked).toBe(true)

    // ⑦ 真交互：清空地址（派发真 `input`）+ 关掉开关（派发真 `change`）。
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = ''
    input.dispatchEvent(new Event('input'))
    const toggle = at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID)
    toggle.checked = false
    toggle.dispatchEvent(new Event('change'))
    await flush()

    // ⑧ 保存 ⇒ PATCH 的 body 是**两个键一起**（地址清空 + 开关停用）。
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()
    expect(callTo('patch', '/api/open/apps/1/').data).toEqual({
      callback_url: '',
      callback_state: 'disabled',
    })
    // ⑨ 回报**说破后果**（地址空 ⇒ 一条都不会推），⛔ 不是"保存成功"四个字了事。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(callbackSaveReport('', 'disabled'))
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('不会推送')
    // ⑩ 列表刷新（保存之后列表上那份数据是新值）。
    expect(LAYUI.reloads).toHaveLength(3)
    expect(tableRows().find((r) => r.id === 1)!.callback_url).toBe('')
  })

  it('名称为空 ⇒ 提示且**不发** POST（再点一次也不会发）', async () => {
    await mount()
    toolbar('add')
    root.querySelector<HTMLInputElement>('#openAppsName')!.value = '   '
    root.querySelector<HTMLButtonElement>('#openAppsCreateSaveBtn')!.click()
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.noName)
    expect(mockedRequest.mock.calls.map((c) => (c[0] as Cfg).method)).not.toContain('post')
  })

  // ── ⭐ 回调地址：即时提示（前端）+ 判据以后端为准 ────────────────

  it('⭐ 回调地址：非 https ⇒ 当场提示；https 或空 ⇒ 不提示（空是**合法值**）', async () => {
    await mount()
    await openCallbackPanel(APP_A)
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    const hint = (): string => at(CALLBACK_HINT_ID).textContent ?? ''
    // 打开时这一行自己就是合法 https ⇒ 没有提示。
    expect(hint()).toBe('')
    // 非 https ⇒ 提示（含 https 这个词：人要看得懂该填什么）。
    input.value = 'http://callback.example.com/tt'
    input.dispatchEvent(new Event('input'))
    expect(hint()).toBe(CALLBACK_URL_HINT)
    expect(hint()).toContain('https')
    // 只有协议头、没有主机 ⇒ 同样提示。
    input.value = 'https://'
    input.dispatchEvent(new Event('input'))
    expect(hint()).toBe(CALLBACK_URL_HINT)
    // 清空 ⇒ **没有提示**（空 = 没配回调，是合法值；⛔ 不许把它说成错的）。
    input.value = ''
    input.dispatchEvent(new Event('input'))
    expect(hint()).toBe('')
    // 那段人话说明也在（https only / 不推送 / 验签名三件事）。
    expect(root.innerHTML).toContain(CALLBACK_NOTICE)
  })

  it('⭐ 判据以后端为准：非法地址后端回 1000 ⇒ 页面把后端那句**显示出来**', async () => {
    await mount()
    ready('PATCH /api/open/apps/1/', () => {
      throw new BizError(1000, '回调地址必须是 https 地址')
    })
    await openCallbackPanel(APP_A)
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = 'http://callback.example.com/tt'
    input.dispatchEvent(new Event('input'))
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()
    // ① 请求**真的发出去了**（前端没把它挡在门外 —— "判据以后端为准"的可证伪形态）。
    expect((callTo('patch', '/api/open/apps/1/').data as Record<string, unknown>).callback_url).toBe(
      'http://callback.example.com/tt',
    )
    // ② 后端那句**如实显示**（⛔ 不是被吞掉、也不是换成一句通用失败）。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe('回调地址必须是 https 地址')
    // ③ 面板**没有**被关掉（用户要能改了再存一次）。
    expect(LAYUI.closeAlls).not.toContain('page')
  })

  it('⭐ 降级分支：行载荷里没有回调那两个键 ⇒ **不打开**面板并说明（⛔ 不当成"没配回调"）', async () => {
    await mount()
    await openCallbackPanel(APP_NO_CALLBACK)
    expect(LAYUI.messages).toContain(MESSAGES.callbackFieldsMissing)
    // ⛔ 关键：没有打开面板 —— 显示成"空的"会让用户一保存就把真实配置覆盖掉。
    expect(LAYUI.opens.filter((o) => String(o.title).startsWith('回调与推送'))).toHaveLength(0)
    // 也没必要去取台账（面板都没开）。
    expect(callCount('/api/open/apps/5/deliveries/')).toBe(0)
  })

  // ── ⭐ 最近推送记录（可折叠 + 重投）───────────────────────────

  it('⭐ 推送记录：六列都在；**只有 failed 行**有「重投」按钮', async () => {
    await mount()
    await openCallbackPanel(APP_A)
    const body = at(DELIVERIES_BODY_ID)
    for (const head of ['时间', '事件', '订单号', '状态', '重试次数', '失败原因']) {
      expect(body.innerHTML).toContain(`>${head}</th>`)
    }
    expect(body.textContent).toContain('订单状态变化')
    expect(body.textContent).toContain('TT202610080001')
    // ⭐ D-0301：主行 = 展示单号（来源号非空取它）；两号不同 ⇒ 副行注「内置 <内置号>」。
    expect(body.textContent).toContain('54556512310200内置 TT202610080001')
    expect(body.textContent).toContain('投递失败')
    expect(body.textContent).toContain('对方回执不是 2xx（HTTP 500）')
    // 值为准：三行都渲染出来了（⛔ 否则"一个按钮都没有"也会让下面那条绿）。
    const trs = body.querySelectorAll('tbody tr')
    expect(trs).toHaveLength(3)
    // 重试次数那一格是**这一行自己的** attempts（失败行 = 7）。
    expect(trs[0]!.children[4]!.textContent).toBe('7')
    // ⭐ 重投按钮**只**在 `status === 'failed'` 那一行（31）：done(32) / pending(33) 都没有。
    const buttons = Array.from(body.querySelectorAll<HTMLElement>(`[${DELIVERY_RETRY_ATTR}]`))
    expect(buttons.map((b) => b.getAttribute(DELIVERY_RETRY_ATTR))).toEqual(['31'])
    expect(trs[1]!.querySelector(`[${DELIVERY_RETRY_ATTR}]`)).toBeNull()
    expect(trs[2]!.querySelector(`[${DELIVERY_RETRY_ATTR}]`)).toBeNull()
  })

  it('⭐ 重投：二次确认 → POST 那条 retry 端点 → **重新拉一次**台账（刷新可见）', async () => {
    await mount()
    await openCallbackPanel(APP_A)
    const before = callCount('/api/open/apps/1/deliveries/')
    const button = root.querySelector<HTMLElement>(`[${DELIVERY_RETRY_ATTR}="31"]`)
    expect(button).not.toBeNull()
    button!.click()
    // ① **还没发** POST：先二次确认（把"重投 = 可能重复推送"的代价说清楚）。
    expect(mockedRequest.mock.calls.map((c) => (c[0] as Cfg).method)).not.toContain('post')
    expect(LAYUI.confirms).toHaveLength(1)
    expect(LAYUI.confirms[0]).toContain('至少一次')
    // ② 点了「确定」才真发。
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('post', '/api/open/apps/1/deliveries/31/retry/')).toBeTruthy()
    // ③ ⭐ **重新拉了一次**台账（不是把本地那行改一改就算了）。
    expect(callCount('/api/open/apps/1/deliveries/')).toBe(before + 1)
    const body = at(DELIVERIES_BODY_ID)
    // ④ 刷新后的内容**确实变了**：那一条现在是「待投递」，重投按钮随之消失。
    expect(body.textContent).toContain('待投递')
    expect(body.querySelector(`[${DELIVERY_RETRY_ATTR}="31"]`)).toBeNull()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('已重投')
  })

  it('⭐ 取台账失败（403）⇒ **如实显示**后端文案，⛔ 不渲染成"还没有推送记录"', async () => {
    await mount()
    ready('GET /api/open/apps/1/deliveries/', () => {
      throw new BizError(403, '无权限执行该操作')
    })
    await openCallbackPanel(APP_A)
    expect(at(DELIVERIES_WARN_ID).textContent).toBe('无权限执行该操作')
    const body = at(DELIVERIES_BODY_ID)
    expect(body.innerHTML).toBe('')
    // ⛔ 关键：没把"取不到"画成"一条都没有"（那是两个方向都会骗人的假信号）。
    expect(body.textContent).not.toContain('还没有推送记录')
  })

  it('「最近推送记录」是可折叠面板：默认收起、点一下展开、再点收起（**真交互**）', async () => {
    await mount()
    await openCallbackPanel(APP_A)
    const body = at(DELIVERIES_BODY_ID)
    const toggle = at(DELIVERIES_TOGGLE_ID)
    expect(body.style.display).toBe('none')
    expect(toggle.textContent).toBe(DELIVERIES_TOGGLE_CLOSED)
    toggle.click()
    expect(body.style.display).toBe('')
    expect(toggle.textContent).toBe(DELIVERIES_TOGGLE_OPEN)
    expect(body.textContent).toContain('TT202610080001')
    toggle.click()
    expect(body.style.display).toBe('none')
    expect(toggle.textContent).toBe(DELIVERIES_TOGGLE_CLOSED)
  })

  it('⭐ 回调那一块套在 `.layui-form[lay-filter]` 里（否则 `lay-skin="switch"` 不会被渲染成开关）', () => {
    // layui 的 `form.render(type, filter)` 根选择器就是 `.layui-form[lay-filter=…]`（读源码实测）
    // ⇒ 少了这一层，标出来的 `lay-skin="switch"` 就是**一句空话**（页面上还是原生复选框）。
    const d = doc(buildHtml())
    const box = d.getElementById(CALLBACK_STATE_SWITCH_ID)!
    expect(box.closest('.layui-form')?.getAttribute('lay-filter')).toBe(CALLBACK_FORM_FILTER)
    expect(box.getAttribute('lay-skin')).toBe('switch')
    expect(box.getAttribute('name')).toBe(CALLBACK_STATE_NAME)
    // 地址输入框在同一块里（一次 `form.render` 覆盖这两个控件）。
    expect(d.getElementById(CALLBACK_URL_INPUT_ID)!.closest('.layui-form')).not.toBeNull()
  })

  it('⭐ 回调面板尺寸沿用 `90%`×`90%`（⛔ 不许改回写死的像素值），台账区能滚', async () => {
    await mount()
    await openCallbackPanel(APP_A)
    const opened = LAYUI.opens[LAYUI.opens.length - 1]!
    // ⚠️ 这一对值是**继承值**：原来取自「角色权限」那条路的权限树弹层（那块已删）⇒
    //    保留既有值，⛔ 不许自己拍一个新的像素值（`AGENTS.md` §5-D）。
    expect(opened.area).toEqual(['90%', '90%'])
    // ⛔ 对照：原来那对"拍出来的"像素值不许回来。
    expect(opened.area).not.toEqual(['760px', '620px'])
    // 台账会随推送变长 ⇒ 容器必须有界 + auto，否则底部的行够不着。
    const body = at(DELIVERIES_BODY_ID)
    expect(body.style.overflow).toBe('auto')
    expect(body.style.maxHeight).not.toBe('')
  })

  // ── 停用 / 启用、轮换 ────────────────────────────────────────

  it('停用：二次确认里说清「第三方当场调不通」，PATCH 只发 state', async () => {
    await mount()
    rowTool(APP_A, 'toggle')
    expect(LAYUI.confirms[0]).toContain('确定停用')
    expect(LAYUI.confirms[0]).toContain('当场')
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('patch', '/api/open/apps/1/').data).toEqual({ state: 'disabled' })
    expect(LAYUI.reloads).toHaveLength(2)
  })

  it('启用：同上，反向', async () => {
    await mount()
    rowTool(APP_B, 'toggle')
    expect(LAYUI.confirms[0]).toContain('确定启用')
    LAYUI.confirmOks[0]!()
    await flush()
    // 应用乙的 pk = 2 ⇒ 打到它自己那条 URL（本次没备响应 = 用例会响）。
    expect(mockedRequest.mock.calls.map((c) => (c[0] as Cfg).url)).toContain('/api/open/apps/2/')
  })

  it('轮换密钥：确认里**保留**"旧密钥立刻作废"，⛔ 删掉"只显示一次"那半句', async () => {
    await mount()
    rowTool(APP_A, 'rotate')
    // ⭐ 二次确认里两件事各自的方向是相反的——
    //    ① 「旧密钥立刻作废」**保留**（这条代价没有放松，spec §9-12）；
    //    ② 「新密钥只显示一次」**没有**（那半句是假的）。
    expect(LAYUI.confirms[0]).toContain('旧密钥立刻作废')
    expect(LAYUI.confirms[0]).not.toContain('只显示一次')
    expect(LAYUI.confirms[0]).not.toContain('只显示这一次')
    // ⛔ 确认框里的文案走 layui 的 HTML 渲染 ⇒ **不许留 markdown 的 `**`**
    //    （用户会连着星号一起看到）。
    expect(LAYUI.confirms[0]).not.toContain('**')
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('post', '/api/open/apps/1/rotate-secret/')).toBeTruthy()
    const opened = LAYUI.opens[LAYUI.opens.length - 1]!
    expect(String(opened.title)).not.toContain('只显示')
    expect(root.querySelector<HTMLTextAreaElement>(`#${SECRET_TEXT_ID}`)!.value).toBe(
      ROTATED.app_secret,
    )
    expect(LAYUI.reloads).toHaveLength(2)
    // ⭐ 列表里给的是**新**密钥（原判据是"列表里没有它"）。
    expect(tableRows().map((r) => r.app_secret)).toContain(ROTATED.app_secret)
    expect(tableRows().map((r) => r.app_secret)).not.toContain(APP_A.app_secret)
  })

  // ── E-0303 / F-01：confirm / msg 的插值是 HTML 上下文，用户可控文本必须逐值转义 ──

  /** 合成载荷（E-0077：不是任何真数据）：同时钉住 `<` `>` `"` 三种转义。 */
  const XSS_NAME = '<img src="x" onerror=window.__xss=1>'
  const XSS_ESCAPED = '&lt;img src=&quot;x&quot; onerror=window.__xss=1&gt;'
  const XSS_APP = (id: number, state: 'enabled' | 'disabled'): ApiAppRow => ({
    ...APP_A,
    id,
    name: XSS_NAME,
    state,
  })

  it('E-0303：停用确认里应用名含 HTML 载荷 ⇒ 上屏的是转义文本，载荷原样不出现', async () => {
    await mount()
    rowTool(XSS_APP(9, 'enabled'), 'toggle')
    expect(LAYUI.confirms[0]).toContain(`确定停用「${XSS_ESCAPED}」`)
    expect(LAYUI.confirms[0]).not.toContain(XSS_NAME)
    expect(LAYUI.confirms[0]).not.toContain('<img')
  })

  it('E-0303：启用确认同上（姊妹位，两条文案各自转义）', async () => {
    await mount()
    rowTool(XSS_APP(9, 'disabled'), 'toggle')
    expect(LAYUI.confirms[0]).toContain(`确定启用「${XSS_ESCAPED}」`)
    expect(LAYUI.confirms[0]).not.toContain(XSS_NAME)
    expect(LAYUI.confirms[0]).not.toContain('<img')
  })

  it('E-0303：轮换确认里应用名同样转义（与「旧密钥立刻作废」那句共存）', async () => {
    await mount()
    rowTool(XSS_APP(9, 'enabled'), 'rotate')
    expect(LAYUI.confirms[0]).toContain(`确定轮换「${XSS_ESCAPED}」的密钥吗？`)
    expect(LAYUI.confirms[0]).toContain('旧密钥立刻作废')
    expect(LAYUI.confirms[0]).not.toContain(XSS_NAME)
    expect(LAYUI.confirms[0]).not.toContain('<img')
  })

  it('E-0303：复制成功的 msg 里应用名同样转义', async () => {
    await mount()
    const writeText = vi.fn((_text: string) => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    rowTool(XSS_APP(9, 'enabled'), 'copysecret')
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(
      `已复制「${XSS_ESCAPED}」的 AppKey 与 AppSecret`,
    )
  })

  // ── 状态按钮的文案（按行预算，⛔ 不是两字并列）────────────────

  it('两个 `state` 不同的行 ⇒ 那颗按钮的文案**逐字不同**（`停用` vs `启用`）', async () => {
    await mount()
    // ① 装配那一刻就按 state 算好了（表里那两行**就是**页面上渲染的行）。
    const listed = tableRows() as Array<ApiAppRow & { state_action_label?: string }>
    expect(listed.map((r) => r.state_action_label)).toEqual(['停用', '启用'])
    // ② 逐字：⛔ 不是"含停用 / 含启用"，也不是两字并列的同一句话。
    expect(stateActionLabel(APP_A)).toBe('停用')
    expect(stateActionLabel(APP_B)).toBe('启用')
    expect(stateActionLabel(APP_A)).not.toBe(stateActionLabel(APP_B))
    // ③ 落到「操作」那一格的标记上：一个 `lay-event="toggle"`（⛔ 不是两个按钮）+ 各自文案。
    const cellA = actionCell(decorateRows([APP_A])[0]!)
    const cellB = actionCell(decorateRows([APP_B])[0]!)
    expect(cellA).toContain('lay-event="toggle">停用</a>')
    expect(cellB).toContain('lay-event="toggle">启用</a>')
    expect(cellA).not.toContain('>启用<')
    expect(cellB).not.toContain('>停用<')
  })

  it('「操作」那一格恰好四个 `lay-event`（callback / copysecret / toggle / rotate），⛔ 没有 del / perm', async () => {
    await mount()
    const cell = actionCell(decorateRows([APP_A])[0]!)
    expect(cell.match(/lay-event="([a-z]+)"/g)).toEqual([
      'lay-event="callback"',
      'lay-event="copysecret"',
      'lay-event="toggle"',
      'lay-event="rotate"',
    ])
  })

  // ── ⛔ 没有删除 ───────────────────────────────────────────────

  it('⛔ 没有删除按钮、也没有删除请求（业务方选定「只能停用不能删」）', async () => {
    await mount()
    // 「操作」那一格现在是**函数 templet**（不在 `buildHtml` 里）⇒ 判据打在它自己的输出上。
    const cell = actionCell(decorateRows([APP_A])[0]!)
    expect(cell).not.toContain('lay-event="del"')
    expect(cell).not.toContain('删除')
    expect(root.innerHTML).not.toContain('lay-event="del"')
    // 行里派的 `del` 事件**什么都没做**（不是"有个按钮忘了接"）。
    mockedRequest.mockClear()
    rowTool(APP_A, 'del')
    await flush()
    expect(mockedRequest).not.toHaveBeenCalled()
    // api 模块里连 delete 方法都不存在（`DELETE` 是 405，spec §7.3 的末行）。
    // ⚠️ 用 `dirname(fileURLToPath(import.meta.url))` + `resolve`，**不是** `new URL('<字面量>', …)`：
    //    目标在 Vite root 之内但那条字面量写法在 vitest 下会被改写成 `http://localhost/...`
    //    ⇒ `fileURLToPath` 抛 `TypeError: The URL must be of scheme file`（仓内既有实测）。
    const src = fs.readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../api/openApps.ts'),
      'utf8',
    )
    expect(src).not.toMatch(/method:\s*'delete'/)
    // ⭐ 那两条已删的写端点（`PUT .../permissions/`、`PUT .../games/`）
    //    也不许在请求侧留下任何调用点 ⇒ 应用管理下**一个 PUT 都没有**。
    expect(src).not.toMatch(/method:\s*'put'/)
  })

  // ── 页面自检那道门（深链）────────────────────────────────────

  it('非账号管理员深链打开 ⇒ 不注入标记（fail-closed）', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 2,
      username: 'synthetic-kefu',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      // 就算**持有**那个节点也不行：路由那第二道门是账号管理员。
      menu_permissions: ['system.pinduoduo_settings'],
      permissions: [],
    })
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    expect(root.innerHTML).toBe('')
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    // 也**没有**去取列表（被拒的页面不该先发一批请求）。
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  // ── 纯函数（即时提示 / 保存回报 / 台账标记 / 时间格）──────────

  it('`callbackUrlHint`：空 / 合法 https ⇒ 无提示；其余 ⇒ 提示（空是**合法值**）', () => {
    expect(callbackUrlHint('')).toBe('')
    expect(callbackUrlHint('   ')).toBe('')
    expect(callbackUrlHint('https://cb.example.com/tt')).toBe('')
    // 大小写不敏感（协议名本来就大小写不敏感）。
    expect(callbackUrlHint('HTTPS://cb.example.com/tt')).toBe('')
    expect(callbackUrlHint('http://cb.example.com/tt')).toBe(CALLBACK_URL_HINT)
    expect(callbackUrlHint('https://')).toBe(CALLBACK_URL_HINT)
    expect(callbackUrlHint('cb.example.com')).toBe(CALLBACK_URL_HINT)
  })

  it('`callbackSaveReport`：三个分支各自**说破后果**（空 / 停用 ⇒ 一条都不会推）', () => {
    expect(callbackSaveReport('', 'enabled')).toContain('不会推送')
    expect(callbackSaveReport('   ', 'enabled')).toContain('不会推送')
    expect(callbackSaveReport('https://cb.example.com/tt', 'disabled')).toContain('停用')
    expect(callbackSaveReport('https://cb.example.com/tt', 'disabled')).toContain('不会推送')
    expect(callbackSaveReport('https://cb.example.com/tt', 'enabled')).toContain('已保存')
    expect(callbackSaveReport('https://cb.example.com/tt', 'enabled')).not.toContain('不会推送')
  })

  it('`timeText` / `lastUsedText`：null 与空串**说人话**（⛔ 不是空白格）', () => {
    expect(timeText(null)).toBe('—')
    expect(timeText('')).toBe('—')
    expect(timeText('2026-10-08T17:20:00+08:00')).toBe('2026-10-08 17:20:00')
    expect(lastUsedText(null)).toContain('还没被调用过')
    expect(lastUsedText('2026-10-07T09:00:00+08:00')).toBe('2026-10-07 09:00:00')
  })

  it('⭐ `deliveriesHtml`：空列表说"还没有"；台账里的字符串一律转义；重投按钮只在 failed 行', () => {
    expect(deliveriesHtml([], 0)).toContain('还没有推送记录')
    // ① 来自后端的字符串一律转义（`reason` 是**对方响应里的原文**，最脏的就是它）。
    const evil: ApiDeliveryRow = {
      ...DELIVERY_FAILED,
      order_no: '<img src=x onerror=alert(1)>',
      reason: '"><b>坏</b>',
      source_order_no: '',
      event_text: '<script>x</script>',
    }
    const html = deliveriesHtml([evil], 1)
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
    expect(html).not.toContain('<b>坏</b>')
    expect(html).not.toContain('<script>')
    // ② 重投按钮的判据是**机器值** `status`：failed 有、done / pending 都没有。
    expect(deliveriesHtml([DELIVERY_FAILED], 1)).toContain(`${DELIVERY_RETRY_ATTR}="31"`)
    expect(deliveriesHtml([DELIVERY_DONE], 1)).not.toContain(DELIVERY_RETRY_ATTR)
    expect(deliveriesHtml([DELIVERY_PENDING], 1)).not.toContain(DELIVERY_RETRY_ATTR)
    // ③ `total` 大于本页条数时**如实说**还差多少（⛔ 不假装这就是全部）。
    expect(deliveriesHtml([DELIVERY_FAILED], 9)).toContain('共 9 条')
    expect(deliveriesHtml([DELIVERY_FAILED], 1)).not.toContain('共 1 条')
  })

  it('`buildHtml`：没有删除入口、没有"只显示一次"的旧措辞，告知与成本 / PII 都在', () => {
    const html = buildHtml()
    expect(html).toContain(SECRET_PANEL_NOTICE)
    expect(html).toContain(COST_NOTICE)
    expect(html).toContain(PII_NOTICE)
    expect(html).toContain('本应用可访问本租户全部业务数据')
    expect(html).not.toContain('lay-event="del"')
    expect(html).not.toContain('<script type="text/html" id="openAppsToolbar">删除')
    // ⛔ 旧口径那几句话**逐字**不许再出现在页面标记里
    //    （留下任何一句都成了"与实现相反的话"，`AGENTS.md` §5-C）。
    expect(html).not.toContain('只显示这一次')
    expect(html).not.toContain('只显示那一次')
    expect(html).not.toContain('再也看不到')
    expect(html).not.toContain('永远不回显')
    expect(html).not.toContain('我已复制，关闭')
    // ⛔ 页面上的中文提示里**不许留 markdown 的 `**`**：这些串是走 `innerHTML` / `textContent`
    //    进 DOM 的，浏览器不认 markdown ⇒ 用户会连着星号一起看到（强调一律用 `<b>`）。
    expect(html).not.toContain('**')
  })

  // ── ⭐ 「API 文档」入口 ────────────────────────
  //
  // 出处是仓库根的 `docs/OPEN-API.md`；页面读的是它在 Vite root 之内的**逐字节副本**
  // `src/docs/open-api.md`（为什么必须有副本，见 `open-apps.ts` 文件头与 `src/docs/AGENTS.md`）。
  // ⇒ 判据分两路：① **两份文件逐字节相同**（下面那条网）；② 页面渲染 / 复制出来的内容
  // 与**仓库根那份**对得上（⛔ 不是"拿页面的输出比它自己"）。

  /** 出处的绝对路径：仓库根的 `docs/OPEN-API.md`（唯一权威）。 */
  const DOC_PATH = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../../../docs/OPEN-API.md',
  )
  /** 页面真正 import 的那份副本。 */
  const DOC_COPY_PATH = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../docs/open-api.md',
  )
  /** 文件此刻的内容（每次现读：弄红证明会临时改文件，读到旧内容就成了假绿）。 */
  function docFile(): string {
    return fs.readFileSync(DOC_PATH, 'utf8')
  }

  it('⛔ 副本网：`src/docs/open-api.md` 与 `docs/OPEN-API.md` **逐字节相同**（漂了就红）', () => {
    // ⚠️ 这条网是**退路写法**的代价所在（`AGENTS.md` §5-C / E-0011 的同族）：
    //    「两份都改还不报错」正是要堵的形态 ⇒ 判据只能是"一模一样"。
    // ⛔ 不许放宽成 `toContain` / 比长度 / 只比前 N 行。
    const original = docFile()
    const copy = fs.readFileSync(DOC_COPY_PATH, 'utf8')
    // 先自证"读到了真东西"（否则空 == 空 也是绿的）。
    expect(original.length).toBeGreaterThan(1000)
    expect(original).toContain('团团通开放平台 API')
    expect(
      copy,
      '两份漂了：请在 `src/frontend/` 下重跑 `cp ../../docs/OPEN-API.md src/docs/open-api.md`',
    ).toEqual(original)
  })

  /**
   * **判据侧的**实体解码（⛔ 刻意不复用页面的 `htmlToText`：那是被测对象）。
   * ⚠️ 一趟扫描（先解 `&amp;lt;` 再解 `&lt;` 那种两趟写法会把 `&amp;lt;` 解成 `<`）。
   */
  const JUDGE_ENTITIES: Record<string, string> = {
    amp: '&',
    lt: '<',
    gt: '>',
    quot: '"',
    apos: "'",
    nbsp: ' ',
  }
  function decodeEntities(s: string): string {
    return s.replace(
      /&(#[xX][0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);/g,
      (whole, body: string) => {
        if (body.startsWith('#')) {
          const hex = body[1] === 'x' || body[1] === 'X'
          const code = Number.parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10)
          return Number.isFinite(code) ? String.fromCodePoint(code) : whole
        }
        const named = JUDGE_ENTITIES[body]
        return named === undefined ? whole : named
      },
    )
  }
  /** 渲染结果 → 纯文本：**先解实体、再去标签**，最后把空白归一化。 */
  function plainText(html: string): string {
    return decodeEntities(html.replace(/<[^>]*>/g, '')).replace(/\s+/g, ' ')
  }

  /**
   * 白名单：渲染结果里允许出现的**标签名**。
   *
   * ⚠️ 这条网是**有意的**（`AGENTS.md` §5-G「结构性检查网自己的断言也必须能失败」）：
   * `marked` 默认**原样放行裸 HTML**，而这份文档是给租户看的页面 ⇒ 谁在文档里写一个裸
   * `<script>`，这里就必须红、逼作者去处理（弄红证明见任务报告；不是靠"应该不会有人写"）。
   */
  const ALLOWED_TAGS = new Set([
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'hr', 'br', 'strong', 'em', 'del', 'a', 'img',
  ])

  /** 打开「API 文档」弹层（工具栏那颗按钮），并返回两个滚动容器。 */
  function openDocPanel(): { toc: HTMLElement; body: HTMLElement } {
    toolbar(DOC_TOOLBAR_EVENT)
    return { toc: at<HTMLElement>(DOC_TOC_ID), body: at<HTMLElement>(DOC_BODY_ID) }
  }

  /** 在 `navigator.clipboard` 上装一个可控替身，返回它。 */
  function stubClipboard(writeText: (text: string) => Promise<void>): void {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  }

  it('⭐ 驱动真交互：点工具栏「API 文档」⇒ 弹层真的打开（DOM 里出现容器 + 目录 + 正文）', async () => {
    await mount()
    // ① 标记里有那颗按钮（工具栏模板是 `buildHtml` 的一部分）。
    expect(buildHtml()).toContain(`lay-event="${DOC_TOOLBAR_EVENT}"`)
    expect(root.querySelector(DOC_SELECTOR)).not.toBeNull()
    // ② 点**之前**：目录与正文都是空的（下面"点之后有内容"才说明是这一次点击干的活）。
    expect(at<HTMLElement>(DOC_TOC_ID).innerHTML).toBe('')
    expect(at<HTMLElement>(DOC_BODY_ID).innerHTML).toBe('')
    expect(LAYUI.opens).toHaveLength(0)

    // ③ 点它。
    const { toc, body } = openDocPanel()

    // ④ 弹层真的开了：`layer.open` 的入参（type 1 = 页面层；尺寸沿用本页既有口径）。
    expect(LAYUI.opens).toHaveLength(1)
    expect(LAYUI.opens[0]).toMatchObject({
      type: 1,
      title: DOC_PANEL_TITLE,
      area: ['90%', '90%'],
    })
    // ⑤ ⭐ 判据打在 **DOM** 上（⛔ 不是"某个函数被调用过"）：目录里有一串条目、正文里有真标题。
    expect(toc.querySelectorAll(DOC_TOC_ITEM_SELECTOR).length).toBeGreaterThan(10)
    expect(toc.querySelector(DOC_TOC_LIST_SELECTOR)).not.toBeNull()
    expect(body.querySelectorAll('h2').length).toBeGreaterThan(0)
    expect(body.querySelectorAll('table').length).toBeGreaterThan(0)
    // ⑥ ⛔ 弹层里**没有** `<script>`：只插 HTML，不给文档内容执行脚本的机会。
    expect(root.querySelector(DOC_SELECTOR)!.querySelectorAll('script')).toHaveLength(0)
  })

  it('⭐ 目录：`##` 一级条目数 == 文档里 `## ` 的行数；`###` 也在，且含已知标题字面量', async () => {
    await mount()
    const { toc } = openDocPanel()
    const raw = docFile()
    // ⚠️ 判据的**左侧**是 fs 读到的原文行数、**右侧**是 `marked` 产物里派生的目录
    //    （两路来源）⇒ 不是自指断言。
    // ⚠️ 已知代价：若将来有人把 `## ` 写进代码块，这一行数会把它也数进去（红一次、人去看即可）。
    const h2Count = raw.split('\n').filter((line) => line.startsWith('## ')).length
    const h3Count = raw.split('\n').filter((line) => line.startsWith('### ')).length
    expect(h2Count).toBeGreaterThanOrEqual(10)
    expect(toc.querySelectorAll('[data-doc-depth="2"]')).toHaveLength(h2Count)
    expect(toc.querySelectorAll('[data-doc-depth="3"]')).toHaveLength(h3Count)
    // 已知标题字面量（⛔ 不是"随便有一项就行"）：一级两处、二级一处。
    const text = toc.textContent ?? ''
    expect(text).toContain('0. 三十秒总览（先读这一段）')
    expect(text).toContain('5. 回调推送契约（我们推给你）')
    expect(text).toContain('1.1 第一步：换令牌')
    // ⚠️ 标题里的 markdown 强调**不该**在目录里露出星号（走 `htmlToText` 去过标签）。
    expect(text).toContain('3. 查单载荷（逐字段冻结）')
    expect(text).not.toContain('**')
  })

  it('⭐ 点目录某一项 ⇒ 跳到对应锚点（滚动的是**那一节**的标题，且高亮跟着走）', async () => {
    await mount()
    const { toc, body } = openDocPanel()
    const links = Array.from(toc.querySelectorAll<HTMLElement>(`[${DOC_ANCHOR_ATTR}]`))
    const linkOf = (label: string): HTMLElement => {
      const found = links.find((a) => (a.textContent ?? '') === label)
      if (!found) throw new Error(`目录里没有「${label}」这一项`)
      return found
    }
    const first = linkOf('1. 接入三步')
    const second = linkOf('5. 回调推送契约（我们推给你）')

    // ① 目录项的锚点**就是**正文里那个标题的 `id`（否则点了跳不过去）。
    const headingOf = (link: HTMLElement): HTMLElement => {
      const anchor = link.getAttribute(DOC_ANCHOR_ATTR)!
      const heading = document.getElementById(anchor)
      if (!heading) throw new Error(`正文里没有锚点 ${anchor}`)
      return heading
    }
    expect(headingOf(first).textContent).toContain('接入三步')
    expect(headingOf(second).textContent).toContain('回调推送契约')
    expect(first.getAttribute(DOC_ANCHOR_ATTR)).not.toBe(second.getAttribute(DOC_ANCHOR_ATTR))

    // ② 真点：jsdom **没有** `scrollIntoView` ⇒ 这里自己装一个（装得上才说明页面真的调了它）。
    const heading = headingOf(first)
    const scrolled = vi.fn()
    heading.scrollIntoView = scrolled as unknown as HTMLElement['scrollIntoView']
    first.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(scrolled).toHaveBeenCalledTimes(1)
    expect(scrolled.mock.calls[0]![0]).toEqual({ block: 'start' })
    // ③ 高亮：目录项一份、标题一份（视觉上"点到了"的证据），且**各只有一处**。
    expect(first.classList.contains(DOC_ACTIVE_CLASS)).toBe(true)
    expect(heading.classList.contains(DOC_ACTIVE_CLASS)).toBe(true)
    expect(toc.querySelectorAll(DOC_ACTIVE_SELECTOR)).toHaveLength(1)
    expect(body.querySelectorAll(DOC_ACTIVE_SELECTOR)).toHaveLength(1)

    // ④ 换一项 ⇒ 高亮**跟着换**（⛔ 不是"永远点亮同一个东西"）。
    const heading2 = headingOf(second)
    heading2.scrollIntoView = scrolled as unknown as HTMLElement['scrollIntoView']
    second.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(scrolled).toHaveBeenCalledTimes(2)
    expect(heading2.classList.contains(DOC_ACTIVE_CLASS)).toBe(true)
    expect(heading.classList.contains(DOC_ACTIVE_CLASS)).toBe(false)
    expect(toc.querySelectorAll(DOC_ACTIVE_SELECTOR)).toHaveLength(1)
  })

  it('⭐ 正文与目录都是**有界可滚动**的容器（⛔ 不把整页撑爆）', async () => {
    await mount()
    const { toc, body } = openDocPanel()
    for (const el of [body, toc]) {
      expect(el.style.overflow).toBe('auto')
      // ⚠️ 有界高度：写死的 px 或视口单位都行，⛔ 不能是空的（空了就等于"撑多高算多高"）。
      expect(el.style.maxHeight).toMatch(/^\d+(px|vh|%)$/)
    }
    // 整页高度由弹层自己的 `area` 决定，组件不占页面高度（容器本身 display:none 收起）。
    expect(root.querySelector(DOC_SELECTOR)!.getAttribute('style')).toContain('display:none')
  })

  it('⭐ 内容来自**真文档**：渲染结果里有 docs/OPEN-API.md 的已知原话', () => {
    const text = plainText(apiDoc().html)
    // 三句都**不含任何 markdown 标记**（含 `**` / 反引号的原话在渲染后会变形，那种不能拿来当判据）。
    expect(text).toContain('拿谁家的密钥就调谁家的域名')
    expect(text).toContain('不要写成"200 就是成功"')
    expect(text).toContain('必须在服务端调')
    // 对照：那句话**确实**来自这个文件（⛔ 不是页面里另抄了一份）。
    const raw = docFile()
    expect(raw).toContain('拿谁家的密钥就调谁家的域名')
    expect(raw).toContain('不要写成"200 就是成功"')
  })

  it('⭐⭐ 尖括号占位符在渲染结果里**还在**（判据：先解实体、再去标签，再在纯文本里找）', () => {
    const html = apiDoc().html
    // ① 前提自证：这些占位符在**文档原文**里就有（否则下面的判据是空跑）。
    const raw = docFile()
    for (const ph of ['<order_no>', '<pk>', '<delivery_pk>']) {
      expect(raw).toContain(ph)
    }
    // ② 渲染器把它们**转义**了 ⇒ 直接在 HTML 字符串里搜字面量是**恒假**的（陷阱，写在这里防改回去）。
    expect(html).toContain('&lt;order_no&gt;')
    expect(html).not.toContain('<order_no>')
    // ③ ⭐ 正判据：**先解实体、再去标签**，在纯文本里找占位符本身。
    //    ⛔ 不许写成 `expect(html).toContain('<order_no>')`（恒假）；
    //    ⛔ 也不许只看 `html.includes('&lt;')` 就放行（那只证明"有转义"，不证明"占位符还在"）。
    const text = plainText(html)
    for (const ph of ['<order_no>', '<pk>', '<delivery_pk>']) {
      expect(text).toContain(ph)
    }
    // ④ 对照：同一条路上有个**真的不转义**的字符（否则"纯文本里能找到"可能只是因为什么都没被转义）。
    expect(text).toContain('Authorization: Bearer <令牌>')
  })

  it('⭐ 白名单标签网：标签名 ⊆ 白名单；且没有事件处理属性 / `javascript:` 协议', () => {
    const html = apiDoc().html
    const names = [...html.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9-]*)/g)].map((m) =>
      m[1]!.toLowerCase(),
    )
    // ⚠️ 先自证"扫到了东西"（否则空集合 ⊆ 白名单是恒真的）。
    expect(names.length).toBeGreaterThan(100)
    expect(new Set(names).size).toBeGreaterThan(10)
    const outside = [...new Set(names)].filter((name) => !ALLOWED_TAGS.has(name))
    // ⚠️ 第二条实参**只写一个模板串**（⛔ 不要用 `+` 拼）：`vitest/valid-expect` 的 `maxArgs=2`
    //    只认"简单的"第二实参，写成二元表达式会被它按"2 个以上实参"报错。
    expect(
      outside,
      `渲染结果里出现了白名单之外的标签：${outside.join(', ')}——文档里写了裸 HTML？要么改文档、要么显式把标签加进 ALLOWED_TAGS 并说明为什么安全`,
    ).toEqual([])
    // ⚠️ 标签名在白名单里**不等于**安全：`<img onerror=…>` 的标签名是 `img`。
    //    ⇒ 这一条是同一张网的另一半，专治"标签合法、属性要命"。
    expect([...html.matchAll(/\son[a-z]+\s*=/gi)].map((m) => m[0])).toEqual([])
    expect(html).not.toMatch(/(?:href|src)\s*=\s*["']?\s*javascript:/i)
    // 对照（⛔ 不是恒真的那一半）：白名单里**确实**有用到的标签，这张网不是"因为一个标签都没有"才绿的。
    expect(new Set(names)).toContain('table')
    expect(new Set(names)).toContain('code')
    expect(new Set(names)).toContain('h2')
  })

  it('⭐ 锚点 id 唯一：重复标题也各有各的 id（唯一性由序号保证，⛔ 不靠 slug 去重）', () => {
    // ⚠️ 合成小文档（与真文档无关）：三处同名、两种层级 ⇒ 专门造 slug 相撞的情形。
    const doc = renderApiDoc('# 标题\n\n## 同名小节\n\n## 同名小节\n\n### 同名小节\n')
    expect(doc.headings).toHaveLength(4)
    const ids = doc.headings.map((h) => h.id)
    expect(new Set(ids).size).toBe(4)
    for (const id of ids) {
      // 每个 id 在正文里**恰好出现一次**（出现两次就是"锚点指到了两处"）。
      expect(doc.html.split(`id="${id}"`)).toHaveLength(2)
    }
    // 目录里的锚点与正文标题的 id **逐字同值**（这是"点了能跳"的结构保证）。
    const toc = docTocHtml(doc.headings)
    for (const h of doc.headings.filter((x) => x.depth === 2 || x.depth === 3)) {
      expect(toc).toContain(`${DOC_ANCHOR_ATTR}="${h.id}"`)
    }
    // ⛔ `#`（文档大标题）与 `####` 不进目录：本次这份文档有 1 个 h1 与 2 个 h4。
    expect(toc).not.toContain(`${DOC_ANCHOR_ATTR}="${ids[0]!}"`)
  })

  it('`htmlToText`：先去标签、再**一趟**解实体（⛔ 两趟会把 `&amp;lt;` 解成 `<`）', () => {
    expect(htmlToText('<strong>逐字段冻结</strong>')).toBe('逐字段冻结')
    expect(htmlToText('&lt;order_no&gt;')).toBe('<order_no>')
    expect(htmlToText('<code>&lt;pk&gt;</code>')).toBe('<pk>')
    // ⭐ 这条就是"一趟扫描"的判据：两趟写法会得到 `<`（错的）。
    expect(htmlToText('&amp;lt;')).toBe('&lt;')
    // 表里没有的实体**原样保留**（⛔ 不猜）。
    expect(htmlToText('&thereisno;')).toBe('&thereisno;')
  })

  it('⭐ 「复制 Markdown 原文」拿到的字符串 == docs/OPEN-API.md 的文件内容（逐字节）', async () => {
    await mount()
    const writeText = vi.fn((_text: string) => Promise.resolve())
    stubClipboard(writeText)
    openDocPanel()
    at<HTMLButtonElement>(DOC_COPY_BUTTON_ID).click()
    await flush()
    expect(writeText).toHaveBeenCalledTimes(1)
    // ⚠️ 判据的两侧是**两路来源**：动作真正写给剪贴板的那个串 vs `fs` 读同一个文件。
    //    页面若改成硬编码字符串 / 抄一份副本，这里就对不上。
    expect(writeText.mock.calls[0]![0]).toBe(docFile())
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.copyDocDone)
  })

  it('⭐ 复制文档失败（浏览器不给剪贴板）**说出来并给出路**，⛔ 不静默假装成功', async () => {
    await mount()
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    openDocPanel()
    at<HTMLButtonElement>(DOC_COPY_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.copyDocFailed)
    // ⚠️ 提示里要**给一条出路**（那颗「下载 .md」），⛔ 不是"复制失败"四个字了事。
    expect(MESSAGES.copyDocFailed).toContain('下载 .md')
  })

  it('⭐ 「下载 .md」：Blob + `a[download]`，存下来的就是那份文件；做不到时说人话', async () => {
    await mount()
    openDocPanel()
    // ⚠️ 替身要**声明一个形参**：写成 `vi.fn(() => '…')` 时 `mock.calls` 的元素类型是空元组
    //    `[]` ⇒ 下面取 `calls[0][0]` 会被 `vue-tsc` 报 TS2493。
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:synthetic-doc')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', {
      value: createObjectURL,
      configurable: true,
    })
    Object.defineProperty(URL, 'revokeObjectURL', {
      value: revokeObjectURL,
      configurable: true,
    })
    const clicked: HTMLAnchorElement[] = []
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clicked.push(this)
      })
    try {
      at<HTMLButtonElement>(DOC_DOWNLOAD_BUTTON_ID).click()
      await flush()
      expect(createObjectURL).toHaveBeenCalledTimes(1)
      const blob = createObjectURL.mock.calls[0]![0]
      expect(blob.type).toContain('text/markdown')
      // ⭐ 存下来的内容 == 那份文件（与「复制」那条同一个判据、同一份来源）。
      await expect(blob.text()).resolves.toBe(docFile())
      expect(clicked).toHaveLength(1)
      expect(clicked[0]!.download).toBe(DOC_DOWNLOAD_FILENAME)
      expect(clicked[0]!.href).toBe('blob:synthetic-doc')
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:synthetic-doc')
    } finally {
      clickSpy.mockRestore()
    }

    // ② 降级分支：这个环境**存不了文件** ⇒ 必须说人话并给出路（⛔ 不是点了没反应）。
    LAYUI.messages.length = 0
    Object.defineProperty(URL, 'createObjectURL', { value: undefined, configurable: true })
    at<HTMLButtonElement>(DOC_DOWNLOAD_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.docDownloadFailed)
    expect(MESSAGES.docDownloadFailed).toContain('docs/OPEN-API.md')
    // `downloadTextFile` 自己也要如实回 `false`（调用方靠它决定说不说那句话）。
    expect(downloadTextFile(DOC_DOWNLOAD_FILENAME, 'x')).toBe(false)
  })

  it('⭐ 弹层反复开关**不重跑渲染**：第二次打开目录 / 正文与第一次逐字相同', async () => {
    await mount()
    const { toc, body } = openDocPanel()
    const firstToc = toc.innerHTML
    const firstBody = body.innerHTML
    expect(firstBody.length).toBeGreaterThan(1000)
    // 关掉（`end` 回调）再开一次：`layer.open` 的替身不动节点，`end` 也没被调 ⇒ 手动收一次。
    ;(LAYUI.opens[0]!.end as () => void)()
    openDocPanel()
    expect(LAYUI.opens).toHaveLength(2)
    expect(toc.innerHTML).toBe(firstToc)
    expect(body.innerHTML).toBe(firstBody)
    // 关掉弹层后节点被收回页面（否则第二次打开取不到节点、弹层静默不出现）。
    expect(root.querySelector(DOC_SELECTOR)).not.toBeNull()
  })
})

/** 把一段标记放进一个假 document（照 `user-adduser.spec.ts` 的 `doc()`）。 */
function doc(html: string): Document {
  return new DOMParser().parseFromString(`<div id="root">${html}</div>`, 'text/html')
}
