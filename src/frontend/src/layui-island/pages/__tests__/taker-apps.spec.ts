// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccountRow } from '@/api/accounts'
import { BizError } from '@/api/errors'

/**
 * 【开放平台 → 打手对接】island 页的契约测试（T4，2026-10-08 建）。
 *
 * 这一页是**自有功能**（对标物里没有它，spec §10-7），所以没有证据件可对拍
 * ⇒ 按 `AGENTS.md` §5-G「自建 island 页必须有一条**驱动真交互**的页测试」，
 * 本文件钉的是**有真实后果**的约定：
 *
 * 1. **驱动真交互**（点按钮、点行内动作、走完整条链）：
 *    新建（点工具栏 → 填名字 → 点创建 → POST → 列表刷新）、
 *    停用 / 启用（行内动作 → 二次确认 → PATCH → 刷新）、
 *    轮换密钥（行内动作 → 二次确认 → POST rotate → 刷新）、复制密钥（写剪贴板）。
 * 2. ⭐⭐ **身份那一半**（spec §6，本页最承重的一段）：
 *    持 `system.allow_taker_open_api` 的**打手** ⇒ 页面上**没有**"绑定打手"选择器、
 *    创建时的载荷**不带** `taker`；**管理员** ⇒ 选择器**在**、**必须选**一个才发得出去
 *    （⛔ 不许"没选也照发"——后端会回 `1000`，而用户看不出为什么）。
 * 3. **四条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量、以 `(` 开头的语句前置分号、
 *    先挂样式 → 再塞标记 → 最后 `initIslandElements`（以**运行期 DOM** 判断，不是读源码）、
 *    页里有定时器时假定时器要先于 `setInterval` 包装且用例自己收口
 *    （⚠️ 本页**没有**定时器 ⇒ 这一条在这里体现为「各用例自己 `document.body.innerHTML = ''` 收口」）。
 * 4. ⛔ **没有删除、也没有"改绑定"**：页面上没有删除入口、没有 `del` 事件、
 *    `PATCH` 的载荷里**绝不出现** `taker`。
 * 5. ⛔ **标记里没有密钥**（密钥只在一个文本节点里上屏 + 从**行数据**复制进剪贴板）。
 * 6. ⛔ **门禁**：不持本页节点的账号深链打开 ⇒ **不注入标记**（fail-closed）。
 * 7. ⭐ **第二期（2026-10-09 回调线 T6）**：「回调与推送」面板（回调地址 + 开关 ⇒ 一次 `PATCH`
 *    发两个键）、推送台账（**懒加载**：打开页面/打开面板都不拉，点开折叠区才拉）、
 *    手工重投（⭐ **只有 `failed` 行有按钮**；URL 里那两个 id **从页面载荷取**；失败时
 *    **显示后端那句中文**）。五条判据的完整清单见下面那个大注释块。
 *
 * ## 手法（照 `open-apps.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `table.render` /
 *   `table.reloadData` / `layer.*` 的**入参**才能被断言；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/takerApps` 跑的是**真实现** ⇒
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义；
 * - `vi.mock('@/api/auth')`：本页第一句就 `fetchMe()`（门禁 + 身份分支）⇒ 必须可控。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：应用名 / 账号名 / 密钥都是编的，不是任何真租户的数据。
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
  // ⚠️ 这四个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 前两个数组是**两颗保存按钮都在**这个合取（创建那颗 **且** 回调面板那颗）——
  //    两处标记**都**在塞标记这一步之后才存在，这一条才名副其实（⛔ 不是只看创建那颗）。
  //    后两个数组记**选择器列表命中的元素个数**（0 / 2，同一个元素只算一次）：
  //    它才是"回调面板那颗也有读数"的判据 ——⛔ 布尔版与"两次单查相加"版都证不了这件事
  //    （换成查创建那颗，那两种读数一模一样；实测两种写法下变异都 SURVIVED）。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  const markersSeenByBootstrap: number[] = []
  const markersSeenByInit: number[] = []
  // ⚠️ `form.render` 的入参（回调开关那个 `lay-skin="switch"` 要被渲染成开关就靠它）：
  //    少了这一条，"把 `success: () => renderCallbackForm()` 删掉"那种**静默**退化
  //    （页面上退回一个原生复选框）没有任何用例会红。
  const formRenders: Array<{ type?: string; filter?: string }> = []
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
    markersSeenByBootstrap,
    markersSeenByInit,
    formRenders,
    form: {
      render: vi.fn((type?: string, filter?: string) => {
        formRenders.push({ type, filter })
      }),
    },
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
    // `layer.open({content: $(panel)})` 用的那个 `$`：返回一个不抛的替身。
    $: vi.fn((el: HTMLElement) => ({ el, slideUp: vi.fn(), slideDown: vi.fn() })),
  }
})

vi.mock('@/layui/island', () => {
  // ⚠️ 探针记的是**选择器列表命中的元素个数**（`querySelectorAll` 对同一个元素只算一次）：
  //    0 = 标记还没塞；2 = 创建那颗与回调面板那颗**两颗不同**的按钮都在。
  //    ⛔ 不写成布尔、⛔ 也不写成"两次单查相加"：那两种写法都挡不住"把回调面板那颗的
  //    选择器改回创建那颗"这种退化（实测：单查相加会把**同一颗**数成两颗 ⇒ 那条变异 SURVIVED）。
  //    ⚠️ 选择器写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值
  //    **之前**就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const MARKER_SELECTOR = '#takerAppsCreateSaveBtn, #takerAppsCallbackSaveBtn'
  const markerCount = (): number => document.querySelectorAll(MARKER_SELECTOR).length
  return {
    bootstrapIsland: vi.fn(async () => {
      const found = markerCount()
      LAYUI.seenByBootstrap.push(found === 2)
      LAYUI.markersSeenByBootstrap.push(found)
      return { table: LAYUI.table, layer: LAYUI.layer, form: LAYUI.form, $: LAYUI.$ }
    }),
    initIslandElements: vi.fn(() => {
      const found = markerCount()
      LAYUI.seenByInit.push(found === 2)
      LAYUI.markersSeenByInit.push(found)
    }),
    islandMsg: (_layer: unknown, text: string) => {
      LAYUI.messages.push(text)
    },
    // ⚠️ 真实现（两行）：面板收节点要用它 —— 桩成 `vi.fn()` 会让"第二次打开取不到节点"
    //    这条**静默**缺陷从测试里消失。
    reopenGuard: (node: HTMLElement, host: HTMLElement) => {
      if (node.parentElement !== host) host.appendChild(node)
    },
  }
})

/** 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

/** 身份：本页第一句就 `fetchMe()`（门禁 + 身份分支）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  ADMIN_SCOPE_NOTICE,
  AUTHORITY_NOTICE,
  CALLBACK_APP_NAME_ID,
  CALLBACK_FORM_FILTER,
  CALLBACK_HINT_ID,
  CALLBACK_NOTICE,
  CALLBACK_SAVE_BUTTON_ID,
  CALLBACK_SELECTOR,
  CALLBACK_STATE_NAME,
  CALLBACK_STATE_SWITCH_ID,
  CALLBACK_URL_HINT,
  CALLBACK_URL_INPUT_ID,
  CREATE_SAVE_BUTTON_ID,
  DELIVERIES_BODY_ID,
  DELIVERIES_TOGGLE_CLOSED,
  DELIVERIES_TOGGLE_ID,
  DELIVERIES_TOGGLE_OPEN,
  DELIVERIES_WARN_ID,
  DOC_ACTIVE_CLASS,
  DOC_ACTIVE_SELECTOR,
  DOC_ANCHOR_ATTR,
  DOC_BODY_ID,
  DOC_COPY_BUTTON_ID,
  DOC_COPY_DONE,
  DOC_COPY_FAILED,
  DOC_DOWNLOAD_BUTTON_ID,
  DOC_DOWNLOAD_FAILED,
  DOC_DOWNLOAD_FILENAME,
  DOC_PANEL_NOTICE,
  DOC_PANEL_TITLE,
  DOC_SELECTOR,
  DOC_TOC_ID,
  DOC_TOC_ITEM_SELECTOR,
  DOC_TOC_LIST_SELECTOR,
  DOC_TOOLBAR_EVENT,
  FORM_SELECTOR,
  MESSAGES,
  NAME_INPUT_ID,
  NO_ACCESS_MESSAGE,
  PAGE_KEY,
  PICKER_HINT_ID,
  ROLE_NOTICE,
  TABLE_ID,
  TAKER_SCOPE_NOTICE,
  TAKER_SELECT_ID,
  TOOLBAR_ID,
  TITLE,
  __testables,
  type TakerAppTableRow,
} from '../taker-apps'
/**
 * ⚠️ 台账那条「重投」按钮的属性名是**上家侧 `open-apps.ts` 的同一个常量**（本页 import 它，
 * 见 `taker-apps.ts` 文件头那段取舍：台账渲染两族共用同一份实现）⇒ 用例也从那里取，
 * ⛔ 不写 `data-retry-delivery` 字面量（那样两头各写一遍，改了没人红）。
 */
import { DELIVERY_RETRY_ATTR } from '../open-apps'
import { CALLBACK_URL_MAX_LENGTH, type TakerAppRow, type TakerDeliveryRow } from '@/api/takerApps'

const {
  actionCell,
  buildHtml,
  takerApiDoc,
  takerApiDocText,
  callbackSaveReport,
  callbackUrlHint,
  candidateLabel,
  candidateOptionsHtml,
  copyCredentialText,
  deliveriesHtml,
  decorateRows,
  lastUsedText,
  pickerHintText,
  secretCell,
  stateActionLabel,
  stateCell,
  timeText,
} = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; data?: unknown; params?: unknown }

/** 响应路由：按 `METHOD URL` 认领。**没有备好响应就抛**（⛔ 不是静默回 undefined）。 */
let routes: Record<string, (data?: unknown) => unknown>

function ready(key: string, fn: (data?: unknown) => unknown): void {
  routes[key] = fn
}

// ── 夹具（**全部合成**）────────────────────────────────────────────────────

const APP_A: TakerAppRow = {
  id: 1,
  name: '合成凭据甲',
  app_key: '0123456789abcdef',
  app_secret: 'SYNTHETIC-secret-A1b2C3d4',
  taker: 7,
  taker_name: '合成打手甲',
  state: 'enabled',
  state_text: '启用',
  ip_allowlist: '',
  // ⭐ 第二期（T5）追加的两键：这一行**启用了回调**（合法 https 地址）。
  callback_url: 'https://callback.example.com/tt',
  callback_state: 'enabled',
  last_used_at: null,
  secret_rotated_at: null,
  created_at: '2026-10-08T10:00:00+08:00',
}

const APP_B: TakerAppRow = {
  id: 2,
  name: '合成凭据乙',
  app_key: 'fedcba9876543210',
  app_secret: 'SYNTHETIC-secret-B5e6F7g8',
  taker: 8,
  taker_name: '合成打手乙',
  state: 'disabled',
  state_text: '停用',
  ip_allowlist: '10.0.0.0/8',
  // 这一行**没配回调**（地址空 + 开关停用 ⇒ 不会推送）。
  callback_url: '',
  callback_state: 'disabled',
  last_used_at: '2026-10-08T09:00:00+08:00',
  secret_rotated_at: '2026-10-08T08:00:00+08:00',
  created_at: '2026-10-07T10:00:00+08:00',
}

const CREATED: TakerAppRow = {
  ...APP_A,
  id: 9,
  name: '合成凭据丙',
  app_secret: 'SYNTHETIC-secret-CREATED-Q1w2E3',
  taker: 7,
  taker_name: '合成打手甲',
  created_at: '2026-10-08T11:00:00+08:00',
}

const ROTATED: TakerAppRow = { ...APP_A, app_secret: 'SYNTHETIC-secret-ROTATED-Z9y8X7' }

/**
 * 台账两行（⭐ **一行 `done` + 一行 `failed`**，判据 2 要「恰好 1 个重投按钮」）：
 * `41` = 投递失败（**唯一**该有「重投」按钮的行）、`42` = 已投递。
 *
 * ⚠️ 这两个 `id` 就是**页面载荷里的值**（判据 1 要求重投那条 URL 的两个 id 都从
 * **页面拿到的载荷**里取，⛔ 不是从数据库对象拼）——用例里一律引这两个常量，
 * ⛔ 不写 `41` / `42` 字面量。
 */
const TAKER_DELIVERY_FAILED: TakerDeliveryRow = {
  id: 41,
  event: 'order.status_changed',
  event_text: '订单状态变化',
  order_no: 'TT202610090001',
  source_order_no: '54556512310300',
  status: 'failed',
  status_text: '投递失败',
  attempts: 7,
  reason: '对方回执不是 2xx',
  response_status: 500,
  next_attempt_at: null,
  created_at: '2026-10-09T17:20:00+08:00',
  processed_at: '2026-10-09T17:30:00+08:00',
}

const TAKER_DELIVERY_DONE: TakerDeliveryRow = {
  id: 42,
  event: 'order.reassigned',
  event_text: '订单被改派',
  order_no: 'TT202610090002',
  source_order_no: '',
  status: 'done',
  status_text: '已投递',
  attempts: 1,
  reason: '',
  response_status: 200,
  next_attempt_at: null,
  created_at: '2026-10-09T17:25:00+08:00',
  processed_at: '2026-10-09T17:25:10+08:00',
}

/**
 * 台账这一份载荷（实测的后端形状，`page_size` = 20、`created_at` 倒序）。
 * ⚠️ 它是**活对象**（`deliveriesOnServer`）——重投之后重新拉必须**看得到变化**，
 * 否则"重拉了一次台账"那条判据只是在断言夹具（上家侧那条用例的同一手法）。
 */
function deliveryPage(items: TakerDeliveryRow[]): {
  items: TakerDeliveryRow[]
  total: number
  page: number
  page_size: number
} {
  return { items, total: items.length, page: 1, page_size: 20 }
}

/** 候选账号两行（合成值）。`/api/accounts/` 的载荷形状 = `Page<AccountRow>`。 */
const CANDIDATE_ROWS = [
  { id: 7, username: 'taker-a', display_name: '合成打手甲' },
  { id: 8, username: 'taker-b', display_name: '合成打手乙' },
] as unknown as AccountRow[]
const CANDIDATE_PAGE = {
  items: CANDIDATE_ROWS,
  total: CANDIDATE_ROWS.length,
  page: 1,
  page_size: 200,
}

/** 打手视野的 `/api/auth/me`（持本页节点、**不是**任何管理员）。 */
const ME_TAKER = {
  id: 7,
  username: 'taker-a',
  display_name: '合成打手甲',
  identity_plane: 'tenant',
  is_tenant_admin: false,
  is_account_admin: false,
  roles: [],
  menu_permissions: ['system.allow_taker_open_api'],
  permissions: [],
}

/** 管理员视野（走"显示全部 + 必须选一个打手"那一支）。 */
const ME_ADMIN = {
  ...ME_TAKER,
  id: 1,
  username: 'synthetic-admin',
  display_name: '合成管理员',
  is_tenant_admin: true,
  is_account_admin: true,
  menu_permissions: ['system.pinduoduo_settings'],
}

/** 列表最后一次灌进表里的数据（`reloadData` 拿到的就是 `decorateRows` 的产物）。 */
function lastTableData(): TakerAppTableRow[] {
  const last = [...LAYUI.reloads].reverse().find((r) => r.id === TABLE_ID)
  if (!last) throw new Error('没有对列表调用过 reloadData')
  return last.data as TakerAppTableRow[]
}

describe('打手对接 island 页（taker-apps）', () => {
  let root: HTMLElement
  /** 服务端**此刻**那份列表（创建 / PATCH / 轮换会改它 ⇒ 刷新之后能看出变化）。 */
  let appsOnServer: TakerAppRow[]
  /** 服务端**此刻**那份候选账号（`GET /api/accounts/`）。 */
  let candidatesOnServer: typeof CANDIDATE_PAGE
  /** 候选列表这一条请求要不要失败（降级分支用）。 */
  let candidatesFail: Error | null
  /** 服务端**此刻**那份推送台账（重投会改它 ⇒ 重拉之后能看出变化）。 */
  let deliveriesOnServer: ReturnType<typeof deliveryPage>

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
    LAYUI.markersSeenByBootstrap.length = 0
    LAYUI.markersSeenByInit.length = 0
    LAYUI.formRenders.length = 0
    for (const key of Object.keys(LAYUI.handlers)) delete LAYUI.handlers[key]
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    mockedFetchMe.mockResolvedValue(ME_TAKER as never)

    routes = {}
    // ⚠️ **列表的响应是"活的"**：创建 / PATCH / 轮换之后重新取列表必须**看得到新值**
    //    （否则"列表里就有密钥"这条判据只是在断言**夹具**，而不是页面的行为）。
    appsOnServer = [APP_A, APP_B]
    candidatesOnServer = CANDIDATE_PAGE
    candidatesFail = null
    ready('GET /api/taker/apps/', () => appsOnServer)
    ready('POST /api/taker/apps/', () => {
      appsOnServer = [...appsOnServer, CREATED]
      return CREATED
    })
    // PATCH 把入参**并回**这一行（PATCH 语义：缺席的键保持库里的值）。
    ready('PATCH /api/taker/apps/1/', (data) => {
      const patch = (data ?? {}) as Partial<TakerAppRow>
      appsOnServer = appsOnServer.map((row) => (row.id === 1 ? { ...row, ...patch } : row))
      return appsOnServer.find((row) => row.id === 1)!
    })
    ready('PATCH /api/taker/apps/2/', () => ({ ...APP_B, state: 'enabled', state_text: '启用' }))
    ready('POST /api/taker/apps/1/rotate-secret/', () => {
      appsOnServer = appsOnServer.map((row) => (row.id === ROTATED.id ? ROTATED : row))
      return ROTATED
    })
    ready('GET /api/accounts/', () => {
      if (candidatesFail !== null) throw candidatesFail
      return candidatesOnServer
    })
    // ⭐ 第二期（T6）：推送台账那两条端点。⚠️ **默认备好**（用例只在自己要造异常时才覆盖）
    //    ——但页面上**没有任何一处会在挂载时自动调它**（懒加载，判据 3 钉住这一点）。
    deliveriesOnServer = deliveryPage([TAKER_DELIVERY_FAILED, TAKER_DELIVERY_DONE])
    // ⚠️ 两个 key 都用**夹具里的 id** 拼（⛔ 不写字面量）：这样"载荷里那个 id 变了、
    //    用例还拿旧 id 断言"这件事不会静默成立。
    ready(`GET /api/taker/apps/${APP_A.id}/deliveries/`, () => deliveriesOnServer)
    ready(
      `POST /api/taker/apps/${APP_A.id}/deliveries/${TAKER_DELIVERY_FAILED.id}/retry/`,
      () => {
      // 重投成功 = 那一行回到「待投递」（后端语义），下次重拉就看得见 ⇒ 重投按钮随之消失。
      const requeued: TakerDeliveryRow = {
        ...TAKER_DELIVERY_FAILED,
        status: 'pending',
        status_text: '待投递',
        attempts: 0,
        next_attempt_at: '2026-10-09T17:31:00+08:00',
      }
      deliveriesOnServer = deliveryPage([requeued, TAKER_DELIVERY_DONE])
      return requeued
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

  async function mount(me: Record<string, unknown> = ME_TAKER): Promise<void> {
    mockedFetchMe.mockResolvedValue(me as never)
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  }

  /**
   * 等几轮任务（组件里的 promise 链要落到断言之前）。
   *
   * ⚠️ **改准（T6）**：原来只放 3 轮微任务，够一期那几条链（一次请求一跳）；
   * 第二期那两条写动作是**多跳**的（`saveCallbackSettings` = PATCH → 重拉列表 → 提示；
   * `confirmRetry` = POST → 重拉台账 → 提示；而 `request` 替身自己还要一跳）⇒
   * 3 轮微任务会在**最后那句提示**还没落地时就断言（读数：`undefined`）。
   * ⇒ 换成放 6 轮**宏任务**（与上家侧 `open-apps.spec.ts` 的 `flush()` 同一手法）。
   * ⚠️ 本页**没有定时器**（陷阱 ④），这里用的是真定时器（文件里没有 `vi.useFakeTimers`）。
   */
  async function flush(): Promise<void> {
    for (let i = 0; i < 6; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  }

  /** 某个 `id` 的节点（弹层借走后仍在文档里 ⇒ 从 root 查得到）。 */
  function at<T extends HTMLElement>(id: string): T {
    const el = root.querySelector<T>(`#${id}`)
    if (!el) throw new Error(`页面上没有 #${id}`)
    return el
  }

  /** 触发一次工具栏事件（layui 的 `toolbar(id)` 委托）。 */
  function toolbar(event: string): void {
    const cb = LAYUI.handlers[`toolbar(${TABLE_ID})`]
    if (!cb) throw new Error('页面没有注册 toolbar 事件')
    cb({ event })
  }

  /** 触发一次行内动作（layui 的 `tool(id)` 委托）。 */
  function rowTool(event: string, row: TakerAppRow): void {
    const cb = LAYUI.handlers[`tool(${TABLE_ID})`]
    if (!cb) throw new Error('页面没有注册 tool 事件')
    cb({ event, data: row })
  }

  /** 最近一次 `layer.confirm` 的确认回调。 */
  function lastConfirm(): string {
    const text = LAYUI.confirms[LAYUI.confirms.length - 1]
    if (text === undefined) throw new Error('没有调用过 layer.confirm')
    return text
  }

  /** 最近一次发往某条 URL 的请求（没有就抛，避免静默空判据）。 */
  function callTo(method: string, url: string): Cfg {
    const found = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .filter((c) => (c.method ?? 'get').toLowerCase() === method && c.url === url)
    if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
    return found[found.length - 1]!
  }

  /** 打到某条 URL 的请求条数（用于"初始化没拉 / 展开才拉 / 重投后重拉一次"这类判据）。 */
  function callCount(url: string): number {
    return mockedRequest.mock.calls.map((c) => c[0] as Cfg).filter((c) => c.url === url).length
  }

  function confirmOk(): void {
    const ok = LAYUI.confirmOks[LAYUI.confirmOks.length - 1]
    if (!ok) throw new Error('没有待确认的 layer.confirm')
    ok()
  }

  // ── 挂载顺序与标记形状（陷阱 ①②③，运行期判据）────────────────────

  it('先挂样式再塞标记：bootstrapIsland 看不到标记、initIslandElements 看得到', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('类名与选择器是两个常量：标记里是 `class="takerAppsForm"`（⛔ 不是 `.takerAppsForm`）', async () => {
    await mount()
    expect(root.querySelector(FORM_SELECTOR)).not.toBeNull()
    // 写成 `class="${FORM_SELECTOR}"` 会造出 `class=".takerAppsForm"`、永远匹配不到。
    expect(root.innerHTML).not.toContain('class=".takerAppsForm"')
    expect(root.querySelector(`#${TABLE_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${NAME_INPUT_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${CREATE_SAVE_BUTTON_ID}`)).not.toBeNull()
    expect(TOOLBAR_ID).toBe('takerAppsToolbar')
  })

  it('island key / 标题 / 表格 id 三处常量对得上（key 必须等于文件名）', async () => {
    expect(PAGE_KEY).toBe('taker-apps')
    expect(TITLE).toBe('打手对接')
    expect(TABLE_ID).toBe('takerAppsTable')
  })

  it('挂载时取一次列表，行**原样**灌进表（只多一个本页预算的文案字段）；密钥**就在行里**', async () => {
    await mount()
    const data = lastTableData()
    expect(data.map((r) => r.id)).toEqual([1, 2])
    expect(data[0]!.app_secret).toBe(APP_A.app_secret)
    expect(data[0]!.state_action_label).toBe('停用')
    expect(data[1]!.state_action_label).toBe('启用')
    // 表只 render 一次（数据走 reloadData）——与先例同形。
    expect(LAYUI.renders.length).toBe(1)
  })

  // ── ⭐⭐ 身份那一半（spec §6）─────────────────────────────────────────

  it('⭐ 打手：**不渲染**「绑定打手」选择器，且页面提示说清"只有你自己的"', async () => {
    await mount(ME_TAKER)
    expect(root.querySelector(`#${TAKER_SELECT_ID}`)).toBeNull()
    expect(root.innerHTML).toContain(TAKER_SCOPE_NOTICE)
    expect(root.innerHTML).not.toContain(ADMIN_SCOPE_NOTICE)
    // ⛔ 不许"渲染出来再禁用"（禁用还看得见，用户会反复试）。
    expect(root.innerHTML).not.toContain(`id="${TAKER_SELECT_ID}"`)
  })

  it('⭐ 打手创建的载荷**不带** `taker`（后端锁死为他自己，spec §6）', async () => {
    await mount(ME_TAKER)
    at<HTMLInputElement>(NAME_INPUT_ID).value = '合成凭据丙'
    at<HTMLButtonElement>(CREATE_SAVE_BUTTON_ID).click()
    await flush()
    const post = mockedRequest.mock.calls.find((c) => (c[0] as Cfg).method === 'post')
    expect(post, '没有发创建请求').toBeDefined()
    const body = (post![0] as Cfg).data as Record<string, unknown>
    expect(body).toEqual({ name: '合成凭据丙' })
    expect('taker' in body).toBe(false)
    // 刷新之后新行在列表里（密钥也在）。
    expect(lastTableData().map((r) => r.id)).toContain(CREATED.id)
  })

  it('⭐⭐ 管理员：选择器**在**、候选被灌进去，且**必须选**一个才发得出去', async () => {
    await mount(ME_ADMIN)
    expect(root.querySelector(`#${TAKER_SELECT_ID}`)).not.toBeNull()
    expect(root.innerHTML).toContain(ADMIN_SCOPE_NOTICE)

    toolbar('add')
    await flush()
    const select = at<HTMLSelectElement>(TAKER_SELECT_ID)
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['', '7', '8'])
    expect(select.value, '默认必须落在空值上（不许顺手建成别人的）').toBe('')

    at<HTMLInputElement>(NAME_INPUT_ID).value = '合成凭据丙'
    at<HTMLButtonElement>(CREATE_SAVE_BUTTON_ID).click()
    await flush()
    expect(mockedRequest.mock.calls.some((c) => (c[0] as Cfg).method === 'post')).toBe(false)
    expect(LAYUI.messages).toContain(MESSAGES.noTaker)

    // 选了之后才发得出去，且载荷里带的是**选中的那个** id。
    select.value = '8'
    at<HTMLButtonElement>(CREATE_SAVE_BUTTON_ID).click()
    await flush()
    const post = mockedRequest.mock.calls.find((c) => (c[0] as Cfg).method === 'post')
    expect(post, '选了账号之后仍没有发创建请求').toBeDefined()
    expect((post![0] as Cfg).data).toEqual({ name: '合成凭据丙', taker: 8 })
  })

  it('⭐ 候选列表取不到 ⇒ **如实说**（⛔ 不渲染成"本租户一个账号都没有"）', async () => {
    candidatesFail = new BizError(403, '当前职位没有此操作所需的权限节点')
    await mount(ME_ADMIN)
    toolbar('add')
    await flush()
    const hint = at<HTMLElement>(PICKER_HINT_ID).textContent ?? ''
    expect(hint).toContain(MESSAGES.pickerFailed)
    expect(hint).toContain('当前职位没有此操作所需的权限节点')
    expect(hint).not.toContain(MESSAGES.pickerEmpty)
  })

  it('⭐ 候选只取了第一页 ⇒ 如实说差多少（⛔ 不假装这就是全部）', async () => {
    candidatesOnServer = { ...CANDIDATE_PAGE, total: 500 }
    await mount(ME_ADMIN)
    toolbar('add')
    await flush()
    const hint = at<HTMLElement>(PICKER_HINT_ID).textContent ?? ''
    expect(hint).toContain('500')
    expect(hint).toContain('2')
  })

  it('⛔ 打手那一副面孔**不会**去打候选列表端点（没有那个选择器就不发那次请求）', async () => {
    await mount(ME_TAKER)
    toolbar('add')
    await flush()
    expect(
      mockedRequest.mock.calls.some((c) => (c[0] as Cfg).url === '/api/accounts/'),
    ).toBe(false)
  })

  // ── 门禁（fail-closed）──────────────────────────────────────────────

  it('不持本页节点的账号深链打开 ⇒ 不注入标记（fail-closed）', async () => {
    await mount({ ...ME_TAKER, menu_permissions: ['order.list'] })
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    expect(root.innerHTML).toBe('')
    expect(LAYUI.renders.length).toBe(0)
  })

  // ── 创建 / 停用 / 轮换 / 复制（**驱动真交互**）──────────────────────

  it('名称为空 ⇒ 提示且**不发** POST', async () => {
    await mount(ME_TAKER)
    at<HTMLButtonElement>(CREATE_SAVE_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.noName)
    expect(mockedRequest.mock.calls.some((c) => (c[0] as Cfg).method === 'post')).toBe(false)
  })

  it('⭐ 新建的点击链：点工具栏 → 填名字 → 点创建 → POST → 关弹层 → 列表刷新', async () => {
    await mount(ME_TAKER)
    toolbar('add')
    await flush()
    expect(LAYUI.opens.length, '点「新增」没有打开弹层').toBe(1)
    expect((LAYUI.opens[0] as { title: string }).title).toContain('新增打手凭据')

    at<HTMLInputElement>(NAME_INPUT_ID).value = '合成凭据丙'
    at<HTMLButtonElement>(CREATE_SAVE_BUTTON_ID).click()
    await flush()

    const post = mockedRequest.mock.calls.find((c) => (c[0] as Cfg).method === 'post')!
    expect((post[0] as Cfg).url).toBe('/api/taker/apps/')
    expect(LAYUI.closeAlls).toContain('page')
    expect(lastTableData().map((r) => r.name)).toContain('合成凭据丙')
  })

  it('停用：二次确认里说清「第三方当场调不通」，PATCH 只发 state', async () => {
    await mount(ME_TAKER)
    rowTool('toggle', decorateRows([APP_A])[0]!)
    expect(lastConfirm()).toContain('停用')
    expect(lastConfirm()).toContain('当场')
    confirmOk()
    await flush()
    const patch = mockedRequest.mock.calls.find((c) => (c[0] as Cfg).method === 'patch')!
    expect((patch[0] as Cfg).url).toBe('/api/taker/apps/1/')
    expect((patch[0] as Cfg).data).toEqual({ state: 'disabled' })
    // ⛔ **绝不**顺手把 `taker` 一起发出去（改绑定 = 不换凭据就换掉整个权限面）。
    expect((patch[0] as Cfg).data).not.toHaveProperty('taker')
  })

  it('启用：同上，反向', async () => {
    await mount(ME_TAKER)
    rowTool('toggle', decorateRows([APP_B])[0]!)
    expect(lastConfirm()).toContain('启用')
    confirmOk()
    await flush()
    const patch = mockedRequest.mock.calls.find((c) => (c[0] as Cfg).method === 'patch')!
    expect((patch[0] as Cfg).url).toBe('/api/taker/apps/2/')
    expect((patch[0] as Cfg).data).toEqual({ state: 'enabled' })
  })

  it('⭐ 轮换密钥：确认里写出「旧密钥立刻作废」，POST 那条 rotate 端点 → 再拉一次列表', async () => {
    await mount(ME_TAKER)
    const before = lastTableData().length
    rowTool('rotate', decorateRows([APP_A])[0]!)
    expect(lastConfirm()).toContain('旧密钥')
    expect(lastConfirm()).toContain('立刻作废')
    expect(lastConfirm()).not.toContain('只显示一次')
    confirmOk()
    await flush()
    const post = mockedRequest.mock.calls.find(
      (c) => (c[0] as Cfg).url === '/api/taker/apps/1/rotate-secret/',
    )
    expect(post, '没有打 rotate 端点').toBeDefined()
    expect((post![0] as Cfg).method).toBe('post')
    // 刷新之后**新密钥**在列表里（响应是"活的"⇒ 这条判据测的是页面真的重拉了一次）。
    expect(lastTableData()).toHaveLength(before)
    expect(lastTableData().find((r) => r.id === 1)!.app_secret).toBe(ROTATED.app_secret)
  })

  it('⭐ 复制密钥：写进剪贴板的是 AppKey + AppSecret 两行（从**行数据**取，⛔ 不从 DOM）', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await mount(ME_TAKER)
    const row = decorateRows([APP_A])[0]!
    rowTool('copysecret', row)
    await flush()
    expect(writeText).toHaveBeenCalledWith(copyCredentialText(APP_A))
    expect(LAYUI.messages.some((m) => m.includes('已复制'))).toBe(true)
    // ⛔ 标记里**没有**密钥（密钥只在一个文本节点里上屏）。
    expect(root.innerHTML).not.toContain(APP_A.app_secret)
  })

  it('⭐ 复制失败（浏览器不给剪贴板）**说出来**，⛔ 不静默假装成功', async () => {
    // jsdom 里 `navigator.clipboard` 默认**不存在** ⇒ 走失败那一支（先证"接上了"）。
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    await mount(ME_TAKER)
    rowTool('copysecret', decorateRows([APP_A])[0]!)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.copyFailed)
    expect(LAYUI.messages.some((m) => m.includes('已复制'))).toBe(false)
  })

  // ── E-0303 / F-01：confirm / msg 的插值是 HTML 上下文；应用名是打手自建的自由文本 ──
  // ⚠️ 这一组同时钉「双断言」：常量文案里的字面 `<b>` 必须保留（那是给用户看的加粗），
  //    插值进来的名字必须转义——⛔ 整串 escape 会毁掉前者，漏 escape 会留下后者。

  /** 合成载荷（E-0077：不是任何真数据）：同时钉住 `<` `>` `"` 三种转义。 */
  const XSS_NAME = '<img src="x" onerror=window.__xss=1>'
  const XSS_ESCAPED = '&lt;img src=&quot;x&quot; onerror=window.__xss=1&gt;'
  const XSS_APP = (id: number, state: 'enabled' | 'disabled'): TakerAppRow => ({
    ...APP_A,
    id,
    name: XSS_NAME,
    state,
    state_text: state === 'enabled' ? '启用' : '停用',
  })

  it('E-0303：停用确认——应用名转义，**且**文案里字面的 `<b>当场</b>` 原样保留', async () => {
    await mount(ME_TAKER)
    rowTool('toggle', decorateRows([XSS_APP(9, 'enabled')])[0]!)
    expect(lastConfirm()).toContain(`确定要停用「${XSS_ESCAPED}」吗？`)
    expect(lastConfirm()).toContain('第三方<b>当场</b>调不通')
    expect(lastConfirm()).not.toContain(XSS_NAME)
    expect(lastConfirm()).not.toContain('<img')
  })

  it('E-0303：启用确认同上（姊妹位）', async () => {
    await mount(ME_TAKER)
    rowTool('toggle', decorateRows([XSS_APP(9, 'disabled')])[0]!)
    expect(lastConfirm()).toContain(`确定要启用「${XSS_ESCAPED}」吗？`)
    expect(lastConfirm()).not.toContain(XSS_NAME)
    expect(lastConfirm()).not.toContain('<img')
  })

  it('E-0303：停用成功的 msg 里应用名转义', async () => {
    await mount(ME_TAKER)
    ready('PATCH /api/taker/apps/9/', () => XSS_APP(9, 'disabled'))
    rowTool('toggle', decorateRows([XSS_APP(9, 'enabled')])[0]!)
    confirmOk()
    await flush()
    const last = LAYUI.messages[LAYUI.messages.length - 1]
    expect(last).toBe(`已停用「${XSS_ESCAPED}」`)
  })

  it('E-0303：轮换确认——应用名转义，**且**字面的 `<b>立刻作废</b>` / `<b>当场调不通</b>` 原样保留', async () => {
    await mount(ME_TAKER)
    rowTool('rotate', decorateRows([XSS_APP(9, 'enabled')])[0]!)
    expect(lastConfirm()).toContain(`确定要轮换「${XSS_ESCAPED}」的密钥吗？旧密钥<b>立刻作废</b>`)
    expect(lastConfirm()).toContain('第三方<b>当场调不通</b>')
    expect(lastConfirm()).not.toContain(XSS_NAME)
    expect(lastConfirm()).not.toContain('<img')
  })

  it('E-0303：轮换成功的 msg 与复制成功的 msg 里应用名都转义', async () => {
    await mount(ME_TAKER)
    ready('POST /api/taker/apps/9/rotate-secret/', () => XSS_APP(9, 'enabled'))
    rowTool('rotate', decorateRows([XSS_APP(9, 'enabled')])[0]!)
    confirmOk()
    await flush()
    expect(LAYUI.messages).toContain(`已轮换「${XSS_ESCAPED}」的密钥（新密钥在列表里）`)
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    rowTool('copysecret', decorateRows([XSS_APP(9, 'enabled')])[0]!)
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(
      `已复制「${XSS_ESCAPED}」的 AppKey 与 AppSecret`,
    )
  })

  it('⭐ 写动作失败（403）⇒ **如实显示**后端文案，⛔ 不静默', async () => {
    await mount(ME_TAKER)
    ready('PATCH /api/taker/apps/1/', () => {
      throw new BizError(1000, '应用不存在')
    })
    rowTool('toggle', decorateRows([APP_A])[0]!)
    confirmOk()
    await flush()
    expect(LAYUI.messages).toContain('应用不存在')
  })

  // ── ⛔ 没有删除 / 没有改绑定 ────────────────────────────────────────

  it('「操作」那一格恰好四个 `lay-event`（callback / copysecret / toggle / rotate），⛔ 没有 del', async () => {
    const html = actionCell(decorateRows([APP_A])[0]!)
    const events = [...html.matchAll(/lay-event="([a-z]+)"/g)].map((m) => m[1])
    // ⭐ 第二期（T6）加了 `callback`（「回调与推送」）⇒ 集合从三个变四个。
    //    ⚠️ 仍然是**恰好集合**（⛔ 没有放宽成 `toContain`）：再加一个入口就要同一次改这一行。
    expect(events).toEqual(['callback', 'copysecret', 'toggle', 'rotate'])
    expect(html).not.toContain('del')
  })

  it('⛔ 整页标记里**没有删除入口**（判据打在**标记**上：工具栏事件恰好 add / refresh 两个）', () => {
    for (const show of [true, false]) {
      const html = buildHtml(show)
      const events = [...html.matchAll(/lay-event="([a-z]+)"/g)].map((m) => m[1])
      expect(events).toEqual(['add', 'refresh', 'apidoc'])
      expect(html).not.toContain('lay-event="del"')
      // ⛔ 也没有"改绑定"的输入口：`taker` 只能在**创建时**选定一次（spec §6）。
      expect(html).not.toContain('lay-event="rebind"')
    }
    // 打手那一副面孔里连选择器都不存在（管理员那一副是**创建时**用的，不是行内编辑）。
    expect(buildHtml(false)).not.toContain(`id="${TAKER_SELECT_ID}"`)
  })

  it('⭐ 凭据的能力边界与"节点一改就静默跟着变"两句都在页面上（⛔ 不做隐性默认）', async () => {
    await mount(ME_TAKER)
    expect(root.innerHTML).toContain(AUTHORITY_NOTICE)
    expect(root.innerHTML).toContain(ROLE_NOTICE)
  })

  // ══════════════════════════════════════════════════════════════════════════
  // ⭐ 第二期（回调线 T6）：「回调与推送」面板 / 推送台账（懒加载）/ 手工重投
  //
  // ## 这一块的七条判据（任务书 T6 逐条对应）
  //
  // 1. ⭐ **驱动真交互**：点行内「回调与推送」⇒ 面板真出现；填地址 + 拨开关 + 点保存
  //    ⇒ 断言**发出的请求体**里两键与页面上的值一致（⛔ 不是断言"按钮存在"）；
  //    拉台账 ⇒ 断言渲染出的**行数与内容来自返回载荷**；点某失败行的「重投」⇒ 断言请求 URL
  //    带**那两个 id**（`APP_A.id` 与**载荷里那一行**的 `id`，⛔ 不从数据库对象拼）。
  // 2. ⭐ **只有 `failed` 行有重投按钮**：一行 `done` + 一行 `failed` ⇒ 断言按钮**恰好 1 个**
  //    （⛔ 不是 `toContain`）。
  // 3. ⭐ **打开页面时不拉台账**（懒加载）：初始化后**没有**那条请求；点开折叠区才拉。
  // 4. **四条 island 陷阱**（`AGENTS.md` §5-G，下面「陷阱」那两条用例逐条点名）：
  //    ① 类名与选择器分成两个常量（**回调面板那个新 class 也要**：见下面那条用例）；
  //    ② 以 `(` 开头的语句一律前置分号（本块新增的 `;(layer.open as …)` / `;(layer.confirm …)`）：
  //      ⚠️ **这条本页没有运行期网**——靠**代码审查 + 与既有页面同款写法**（本页这种以 `(` 开头的
  //      语句共 12 处，全部前置了分号）；本仓 eslint **没有**配 `no-unexpected-multiline`
  //      ⇒ ⛔ 不许把它写成"已由用例覆盖"（那是假保证；ASI 这一族只能靠人看）。
  //    ③ `await bootstrapIsland(...)` 先挂样式 → 再塞标记 → 最后 `initIslandElements`
  //      （运行期读数：`seenByBootstrap` 那对是"两颗都在"的布尔，
  //       `markersSeenByBootstrap` 那对记**命中的元素个数** 0 → 2
  //       ⇒ 回调面板那颗也有读数）；
  //    ④ 本页**没有定时器** ⇒ 这一条落在「每个用例自己 `document.body.innerHTML = ''` 收口」上
  //      （⚠️ 这条尤其要紧：上一个用例挂载的页面会继续打**同一个** `vi.mock` 替身，
  //       表现为「关着还在刷」的**假红**）——由 `afterEach` 覆盖。
  // 5. **每条判据都要能红**：两条变异读数（改坏台账按钮的判据 / 改掉懒加载）见本任务报告；
  //    ⚠️ 下面每条用例都配了「把它弄红会怎样」的注释（哪一处改动会让它失败）。
  // 6. ⭐ **重投成功那句回报兑现"至少一次"**（判据 1f）：那句自己的字面值里必须有
  //    「至少一次」/「再收到一次」/「X-TT-Delivery」去重（⛔ 不是只钉"那句非空"、
  //    也⛔ 不是只钉常量引用——那样把这半句删掉不会红）。
  // 7. ⭐ **保存回调失败的网**（判据 1b″）：`PATCH` 回 `1000` + 一句**只有后端会给**的合成中文
  //    ⇒ 断言那句**原样上屏**，且⛔ 不是前端自造的兜底文案（`MESSAGES.failed`）。
  // ══════════════════════════════════════════════════════════════════════════

  /**
   * 打开某一行的「回调与推送」面板（走**行内动作**，与用户点那颗按钮同一条路）。
   *
   * ⚠️ 顺带把 `layer.open` 的 `success` 回调调一次：真 layui 在**内容就绪之后**会调它
   * （本页用它 `form.render` 那个开关）——替身不会自己调，不补这一下，
   * 「开关到底渲没渲染」这条就永远测不到（`open-apps.spec.ts` 今天没补，所以那边那一条
   * 是**没有覆盖**的；本页补上，不去动那一页）。
   */
  function openCallbackPanel(row: TakerAppRow): void {
    rowTool('callback', decorateRows([row])[0]!)
    const opened = [...LAYUI.opens].reverse().find((o) => String(o.title).startsWith('回调与推送'))
    const success = (opened as { success?: () => void } | undefined)?.success
    if (typeof success === 'function') success.call(opened)
  }

  it('⭐ 陷阱①（新面板的 class）：标记里是 `class="takerAppsCallback"`，⛔ 不是 `.takerAppsCallback`', async () => {
    await mount()
    // 写成 `class="${CALLBACK_SELECTOR}"` 会造出 `class=".takerAppsCallback"`、永远匹配不到
    // ⇒ 面板打不开，而 `typecheck` / `lint` 都看不见这件事（`AGENTS.md` §5-G 陷阱 ①）。
    expect(root.querySelector(CALLBACK_SELECTOR)).not.toBeNull()
    expect(root.innerHTML).not.toContain('class=".takerAppsCallback"')
    // ⛔ 也不与上家侧那页共用 class / id（两页可能同时挂在同一个 iframe 壳里）。
    expect(root.innerHTML).not.toContain('openApps')
  })

  it('⭐ 陷阱③：先挂样式再塞标记（回调面板的节点也在标记里，`bootstrapIsland` 看不到）', async () => {
    await mount()
    // 运行期判据（⛔ 不是读源码）：**选择器列表命中的元素个数** —— 样式那一步 0 个，
    // 塞完标记之后 2 个（创建那颗 + 回调面板那颗，**两颗不同**的按钮）。
    // ⇒ 这个面板的标记要是被挪走（懒渲染 / 塞进 `layer.open` 的内容里 / 选择器被改回
    //    创建那颗 ⇒ 命中同一个元素只算一次，读数掉成 1），本用例红。
    expect(LAYUI.markersSeenByBootstrap).toEqual([0])
    expect(LAYUI.markersSeenByInit).toEqual([2])
    // ⚠️ 布尔那一对（`seenByBootstrap` / `seenByInit`）由上面那条"先挂样式再塞标记"用例。
    // ⚠️ 能红（本任务报告的变异读数 M3）：把探针的 `MARKER_SELECTOR` 里
    //    `#takerAppsCallbackSaveBtn` 改成 `#takerAppsCreateSaveBtn` ⇒ 读数变 `[1]` ⇒ 本用例红
    //    （顺带把上面那条布尔用例也打红：那个合取里少了一颗真的）；
    //    ⚠️ 实测：写成"只记布尔"或"两次单查相加"时同一条变异 SURVIVED
    //    （后者会把**同一颗**数成两颗）—— 所以这里必须记"**不同**元素命中个数"。
  })

  it('⭐ 判据 1a：点行内「回调与推送」⇒ 面板真打开、并按**这一行自己**的配置回填', async () => {
    await mount()
    openCallbackPanel(APP_A)
    expect(LAYUI.opens).toHaveLength(1)
    expect(String(LAYUI.opens[0]!.title)).toContain(`回调与推送：${APP_A.name}`)
    // ⛔ 尺寸不许自己拍像素值（本页沿用既有面板那对 `90%`×`90%`）。
    expect(LAYUI.opens[0]!.area).toEqual(['90%', '90%'])
    // 回填 = **这一行自己**的配置（⛔ 不是空、也不是另一行的）。
    expect(at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value).toBe(APP_A.callback_url)
    expect(at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID).checked).toBe(true)
    expect(at(CALLBACK_APP_NAME_ID).textContent).toBe(APP_A.name)
    // 另一行（没配回调）⇒ 空地址 + 关着的开关（判据打在**值**上，⛔ 不是"控件存在"）。
    LAYUI.opens.length = 0
    openCallbackPanel(APP_B)
    expect(at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value).toBe('')
    expect(at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID).checked).toBe(false)
  })

  it('⭐ 判据 1b：填地址 + 拨开关（真事件）⇒ PATCH 的 **body 两键** 与页面上的值一致', async () => {
    await mount()
    openCallbackPanel(APP_A)
    // 真交互：改输入框 + 派发 `input`；改勾选态 + 派发 `change`。
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = '  https://hooks.example.com/taker  '
    input.dispatchEvent(new Event('input'))
    const toggle = at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID)
    toggle.checked = false
    toggle.dispatchEvent(new Event('change'))
    await flush()

    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()

    // ⭐ 判据是**发出的请求体**（⛔ 不是"按钮存在"）：两个键一起发、值与页面一致
    //    （地址按 `trim()` 后的值发 —— 这也是"页面上的值"的规范形态）。
    const sent = callTo('patch', `/api/taker/apps/${APP_A.id}/`)
    expect(sent.data).toEqual({
      callback_url: 'https://hooks.example.com/taker',
      callback_state: 'disabled',
    })
    // 回报**说破后果**（开关停用 ⇒ 一条都不会推），⛔ 不是"保存成功"四个字了事。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(
      callbackSaveReport('https://hooks.example.com/taker', 'disabled'),
    )
    // ⚠️ 能红：把 `state` 从 `callbackApp.callback_state`（打开时的快照）推 ⇒ 这里会发出
    //    旧值 `'enabled'`，本断言红；把 url 直接取值不 `trim()` ⇒ 带空格的串，本断言红。
  })

  it('⭐ 判据 1b′：开关**打开了**就发 `enabled`（⛔ 不是恒发 `disabled`）', async () => {
    await mount()
    openCallbackPanel(APP_B) // 这一行本来是关着的
    at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value = 'https://hooks.example.com/other'
    const toggle = at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID)
    toggle.checked = true
    toggle.dispatchEvent(new Event('change'))
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()
    // ⚠️ 这一条把"两个方向都发得对"钉住：只发 `disabled` 也能让上一条绿。
    // ⚠️ 能红：把开关那一支写死成 `'disabled'`（或读 `row.callback_state`）⇒ 本断言红。
    expect(callTo('patch', `/api/taker/apps/${APP_B.id}/`).data).toEqual({
      callback_url: 'https://hooks.example.com/other',
      callback_state: 'enabled',
    })
  })

  it('⭐ 判据 1b″（保存失败）：`PATCH` 回 `1000` ⇒ **后端那句中文原样上屏**，⛔ 前端不编第二句', async () => {
    await mount()
    openCallbackPanel(APP_A)
    // ⭐ 这是**只有后端会给**的一句合成文案（第二期 §8-4 的判据在后端；故意带上「（合成判据）」
    //    这四个字：任何前端自造的兜底/美化文案都不可能巧合地等于它）。
    //    ⛔ 别把它抄成前端常量——它必须**从响应里取**（`e.message`），抄了就等于把
    //    "后端说了什么"换成"前端以为后端会说什么"。
    const backendMsg = '回调地址必须是 https 公网地址（合成判据）'
    ready(`PATCH /api/taker/apps/${APP_A.id}/`, () => {
      throw new BizError(1000, backendMsg)
    })
    // ⚠️ 先证"这一句不是页面上本来就有的字"（否则下面那条断言可能命中的是页面自己的文案）。
    expect(root.innerHTML).not.toContain(backendMsg)
    expect(LAYUI.messages).toHaveLength(0)

    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = 'http://hooks.example.com/tt'
    input.dispatchEvent(new Event('input'))
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()

    // ① 那句**原样**上屏（逐字，⛔ 不是"包含某个词"）。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(backendMsg)
    // ② ⛔ 也不是前端那份兜底文案（判别"显示的是后端那句"与"显示了同一句巧合"）。
    expect(LAYUI.messages).not.toContain(MESSAGES.failed)
    // ③ 失败了就**没有**"已保存…"那种成功回报（⛔ 不许把失败说成保存成功）。
    expect(LAYUI.messages.some((m) => m.startsWith('已保存'))).toBe(false)
    // ⚠️ 能红：把 `saveCallbackSettings` 的 `.catch((e: Error) => msg(e.message))` 换成
    //    `msg(MESSAGES.failed)`（前端自造一句）⇒ 第 ① 条红；换成 `msg('操作失败')` 同理。
    // ⚠️ 另一条：那条即时提示（`callbackUrlHint`）**不是闸门** —— 上面那个 `http://` 地址
    //    照样发出去了（`ready` 那条路由接得到才走到本断言），否则这一段会以"没有发出…"失败。
    expect(callTo('patch', `/api/taker/apps/${APP_A.id}/`).data).toEqual({
      callback_url: 'http://hooks.example.com/tt',
      callback_state: 'enabled',
    })
  })

  it('⭐ 判据 1c：拉台账 ⇒ 渲染出的**行数与内容都来自返回载荷**', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()

    // 请求形态：第一页、每页 20（第二期 §7 第二行）。
    const sent = callTo('get', `/api/taker/apps/${APP_A.id}/deliveries/`)
    expect(sent.params).toEqual({ page: 1, page_size: 20 })
    // 行数 = 载荷的 `items.length`（⛔ 不是写死 2）。
    const trs = at(DELIVERIES_BODY_ID).querySelectorAll('tbody tr')
    expect(trs).toHaveLength(deliveriesOnServer.items.length)
    // 内容逐格来自载荷（事件中文名 / 单号 / 状态 / 尝试次数 / 失败原因）。
    const body = at(DELIVERIES_BODY_ID)
    expect(body.textContent).toContain(TAKER_DELIVERY_FAILED.event_text)
    expect(body.textContent).toContain(TAKER_DELIVERY_FAILED.order_no)
    expect(body.textContent).toContain(TAKER_DELIVERY_FAILED.status_text)
    expect(body.textContent).toContain(TAKER_DELIVERY_FAILED.reason)
    expect(trs[0]!.children[4]!.textContent).toBe(String(TAKER_DELIVERY_FAILED.attempts))
    // ⚠️ 能红：把 `deliveriesHtml(page.items, page.total)` 换成 `deliveriesHtml([], 0)`
    //    （或拿 `rows` 之类的本地数据上屏）⇒ 上面几条全红。
  })

  it('⭐ 台账表头文案钉在本页用例里（复用上家侧那份实现也照样要有网）', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    // ⚠️ 值**写在用例里**（⛔ 不从 `open-apps.ts` 引常量）：表头是渲染给打手看的那一列字，
    //    而这一份渲染实现是**两页共用**的 ⇒ 上家侧改了表头，本页会**静默**跟着变。
    //    ⛔ 也不断言"有几列"就算完（列数不变、字换了正是要抓的那种漂移）。
    const heads = Array.from(at(DELIVERIES_BODY_ID).querySelectorAll('thead th')).map(
      (th) => th.textContent,
    )
    expect(heads).toEqual(['时间', '事件', '订单号', '状态', '重试次数', '失败原因', '操作'])
    // 表头与行**逐列对齐**（7 个 `<th>` 就 7 个 `<td>`；错位会让整张表读串行）。
    const firstRow = at(DELIVERIES_BODY_ID).querySelectorAll('tbody tr')[0]!
    expect(firstRow.children).toHaveLength(heads.length)
    // ⚠️ 能红：把 `open-apps.ts::deliveriesHtml` 里 `重试次数` 改成别的字
    //    （或删一列 / 加一列）⇒ 本用例红（本页自己也有一份读数，不是只有上家侧那些用例守）。
  })

  it('⭐ 即时提示跟着输入走（真 `input` 事件）；那段人话说明也在页面上', async () => {
    await mount()
    openCallbackPanel(APP_A)
    // 打开时这一行自己就是合法 https ⇒ 没有提示。
    expect(at(CALLBACK_HINT_ID).textContent).toBe('')
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = 'http://hooks.example.com/tt'
    input.dispatchEvent(new Event('input'))
    expect(at(CALLBACK_HINT_ID).textContent).toBe(CALLBACK_URL_HINT)
    input.value = 'https://hooks.example.com/tt'
    input.dispatchEvent(new Event('input'))
    expect(at(CALLBACK_HINT_ID).textContent).toBe('')
    // ⚠️ 那句提示**不是闸门**：后端拒的时候必须还能发出去（见下面那条「保存失败」用例，
    //    判据 1b″：`PATCH` 回 `1000` + 一句只有后端会给的合成中文 ⇒ 那句原样上屏）。
    // 那段说明要说清三件事：三个头要验签 / 大厅新单不推 / 空或停用不推送。
    expect(root.innerHTML).toContain(CALLBACK_NOTICE)
    expect(CALLBACK_NOTICE).toContain('X-TT-Delivery')
    expect(CALLBACK_NOTICE).toContain('不会推送')
    expect(CALLBACK_NOTICE).not.toContain('**')
  })

  it('⭐⭐ 判据 2：一行 `done` + 一行 `failed` ⇒ 重投按钮**恰好 1 个**（恰好集合，⛔ 不是 toContain）', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    const buttons = Array.from(
      at(DELIVERIES_BODY_ID).querySelectorAll<HTMLElement>(`[${DELIVERY_RETRY_ATTR}]`),
    )
    expect(buttons).toHaveLength(1)
    // 而且就是**failed 那一行**的那个 id（值来自载荷）。
    expect(buttons.map((b) => b.getAttribute(DELIVERY_RETRY_ATTR))).toEqual([
      String(TAKER_DELIVERY_FAILED.id),
    ])
    // ⚠️ 能红（变异读数见报告）：把 `deliveriesHtml` 里那条 `status === 'failed'` 去掉
    //    （任何状态都画按钮）⇒ `toHaveLength(1)` 变成 2 ⇒ 红。
    // ⚠️ 反向也钉住：**别**把按钮画没了（`toHaveLength(1)` 对"一个都没有"同样会红）。
    const trs = at(DELIVERIES_BODY_ID).querySelectorAll('tbody tr')
    expect(trs[1]!.querySelector(`[${DELIVERY_RETRY_ATTR}]`)).toBeNull()
  })

  it('⭐ 判据 1d：点失败行的「重投」⇒ 二次确认 → POST 的 URL 带**载荷里那两个 id** → 重拉台账', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()

    const before = callCount(`/api/taker/apps/${APP_A.id}/deliveries/`)
    // ⭐ 两个 id 都从**页面载荷**里取：应用那个取自行载荷（`APP_A.id` = 表里那一行的 id），
    //    台账那个取自**台账载荷里那一行**（它同时被渲染进了按钮的属性里 ⇒ 下面顺手对拍一次）。
    const appPk = lastTableData().find((r) => r.name === APP_A.name)!.id
    const deliveryPk = deliveriesOnServer.items.find((d) => d.status === 'failed')!.id
    const button = at(DELIVERIES_BODY_ID).querySelector<HTMLElement>(
      `[${DELIVERY_RETRY_ATTR}="${deliveryPk}"]`,
    )
    expect(button, '页面上没有那一行的重投按钮').not.toBeNull()

    button!.click()
    // ① **还没发** POST：先二次确认（把"重投 = 可能重复推送"的代价说清楚）。
    expect(callCount(`/api/taker/apps/${appPk}/deliveries/${deliveryPk}/retry/`)).toBe(0)
    expect(LAYUI.confirms).toHaveLength(1)
    expect(lastConfirm()).toContain('至少一次')

    // ② 点了「确定」才真发。
    confirmOk()
    await flush()
    // ⭐ URL 里**两个 id 都在**（任一个错了都会抛"没有发出…"）。
    expect(callTo('post', `/api/taker/apps/${appPk}/deliveries/${deliveryPk}/retry/`)).toBeTruthy()
    // ③ ⭐ **重新拉了一次**台账（不是把本地那行改一改就算了）。
    expect(callCount(`/api/taker/apps/${appPk}/deliveries/`)).toBe(before + 1)
    // ④ 刷新后的内容**确实变了**：那一条现在是「待投递」，重投按钮随之消失。
    expect(at(DELIVERIES_BODY_ID).textContent).toContain('待投递')
    expect(
      at(DELIVERIES_BODY_ID).querySelector(`[${DELIVERY_RETRY_ATTR}="${deliveryPk}"]`),
    ).toBeNull()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.retryDone)
  })

  it('⭐ 判据 3（懒加载）：**打开页面时不拉台账**；点开折叠区才拉；收起不拉', async () => {
    await mount()
    const url = `/api/taker/apps/${APP_A.id}/deliveries/`
    // ① 挂载后一条都没有（⛔ 不是"拉过一次就够"——初始化整段都不许有）。
    expect(callCount(url)).toBe(0)
    // ② 打开面板**也不拉**（面板开着 ≠ 用户要看台账）。
    openCallbackPanel(APP_A)
    await flush()
    expect(callCount(url)).toBe(0)
    // ③ 点开折叠区 ⇒ 才拉一条。
    const toggle = at(DELIVERIES_TOGGLE_ID)
    expect(at(DELIVERIES_BODY_ID).style.display).toBe('none')
    expect(toggle.textContent).toBe(DELIVERIES_TOGGLE_CLOSED)
    toggle.click()
    await flush()
    expect(callCount(url)).toBe(1)
    expect(at(DELIVERIES_BODY_ID).style.display).toBe('')
    expect(toggle.textContent).toBe(DELIVERIES_TOGGLE_OPEN)
    // ④ 收起**不拉**；再展开才又拉一条（⛔ 不是"关着还在刷"）。
    toggle.click()
    await flush()
    expect(callCount(url)).toBe(1)
    expect(at(DELIVERIES_BODY_ID).style.display).toBe('none')
    toggle.click()
    await flush()
    expect(callCount(url)).toBe(2)
    // ⚠️ 能红（变异读数见报告）：把 `openCallback` 末尾加一句 `await loadDeliveries(...)`
    //    （= 退回上家侧那种"面板一开就取"）⇒ 第 ② 步 `callCount` 变 1 ⇒ 红。
  })

  it('⭐ 判据 3′：**换一个应用再打开**时台账回到收起态，且不残留上一行的行', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    expect(at(DELIVERIES_BODY_ID).querySelectorAll('tbody tr')).toHaveLength(2)
    // 关掉再开另一行（`layer.open` 的替身不动节点、`end` 也没被调 ⇒ 手动收一次）。
    ;((LAYUI.opens[0] as { end: () => void }).end)()
    openCallbackPanel(APP_B)
    expect(at(DELIVERIES_BODY_ID).innerHTML).toBe('')
    expect(at(DELIVERIES_TOGGLE_ID).textContent).toBe(DELIVERIES_TOGGLE_CLOSED)
    // 而且**没有**因此多打一条请求（换面板不是取数动作）。
    expect(callCount(`/api/taker/apps/${APP_B.id}/deliveries/`)).toBe(0)
  })

  it('⭐ 判据 1e（重投失败）：**显示后端那句中文**，⛔ 前端不编第二句', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    // 后端那句原文（`apps/takerapi/api/app_views.py::DELIVERY_RETRY_ONLY_FAILED_MSG`）。
    const backendMsg = '只有投递失败的推送记录可以重投（已投递成功的记录重投会让对方收到两次）'
    // ⚠️ 先证"这一句不是页面上本来就有的字"（否则下面的断言可能命中的是页面自己的文案）。
    expect(root.innerHTML).not.toContain(backendMsg)
    ready(
      `POST /api/taker/apps/${APP_A.id}/deliveries/${TAKER_DELIVERY_FAILED.id}/retry/`,
      () => {
        throw new BizError(1000, backendMsg)
      },
    )
    const deliveryPk = deliveriesOnServer.items.find((d) => d.status === 'failed')!.id
    at(DELIVERIES_BODY_ID)
      .querySelector<HTMLElement>(`[${DELIVERY_RETRY_ATTR}="${deliveryPk}"]`)!
      .click()
    confirmOk()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(backendMsg)
    // ⛔ 页面**没有**把它"美化"成自己的话术 / 通用失败提示。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).not.toBe(MESSAGES.failed)
  })

  it('⭐ 判据 1f（重投成功那句回报）：兑现"至少一次"⇒ 那句里就有「再收到一次」+「X-TT-Delivery」', async () => {
    await mount()
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    const deliveryPk = deliveriesOnServer.items.find((d) => d.status === 'failed')!.id
    at(DELIVERIES_BODY_ID)
      .querySelector<HTMLElement>(`[${DELIVERY_RETRY_ATTR}="${deliveryPk}"]`)!
      .click()
    confirmOk()
    await flush()

    // ⭐ 判据打在**说出口的那句**上（`layer.msg` 的入参），⛔ 不是打在源码常量上。
    const said = LAYUI.messages[LAYUI.messages.length - 1]!
    expect(said).toBe(MESSAGES.retryDone)
    // ⭐ 它自己注释里承诺的**两个后果**都必须在这句里（⛔ 不是"非空"就算数）：
    //    ① 只是**排回队列**、下一轮才发；②「至少一次」⇒ 对方可能**再收到一次**（按头去重）。
    expect(said).toContain('下一轮')
    expect(said).toContain('至少一次')
    expect(said).toContain('再收到一次')
    expect(said).toContain('X-TT-Delivery')
    // ⚠️ 也⛔ 不许把"再收到一次"这层意思**只**留在二次确认里：那句话在用户点完「确定」之后
    //    就没了，用户真正看到的回报就是上面这一句（所以下面这条对着**确认框文案**再核一次，
    //    两边都有 ⇒ 删任何一边都不会让"代价被说出来"这件事整个消失）。
    expect(LAYUI.confirms[0]).toContain('再收到一次')
    // ⚠️ 能红（本任务报告的变异读数）：把 `MESSAGES.retryDone` 里的
    //    「如果对方上次其实收到了，它会「再收到一次」」这半句删掉 ⇒ 本用例红。
  })

  it('⭐ 取台账失败（403）⇒ **如实显示**后端文案，⛔ 不渲染成"还没有推送记录"', async () => {
    await mount()
    ready(`GET /api/taker/apps/${APP_A.id}/deliveries/`, () => {
      throw new BizError(403, '无权限执行该操作')
    })
    openCallbackPanel(APP_A)
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    expect(at(DELIVERIES_WARN_ID).textContent).toBe('无权限执行该操作')
    expect(at(DELIVERIES_BODY_ID).innerHTML).toBe('')
    // ⛔ 关键：没把"取不到"画成"一条都没有"（两个方向都会骗人的假信号）。
    expect(at(DELIVERIES_BODY_ID).textContent).not.toContain('还没有推送记录')
  })

  it('⭐ 降级分支：行载荷里没有回调那两个键 ⇒ **不打开**面板并说明（⛔ 不当成"没配回调"）', async () => {
    await mount()
    const row: Record<string, unknown> = { ...APP_A }
    delete row.callback_url
    delete row.callback_state
    openCallbackPanel(row as unknown as TakerAppRow)
    expect(LAYUI.messages).toContain(MESSAGES.callbackFieldsMissing)
    // ⛔ 关键：没有打开面板 —— 显示成"空的"会让用户一保存就把真实配置覆盖掉。
    expect(LAYUI.opens.filter((o) => String(o.title).startsWith('回调与推送'))).toHaveLength(0)
    // 也没必要去取台账（面板都没开）。
    expect(callCount(`/api/taker/apps/${APP_A.id}/deliveries/`)).toBe(0)
  })

  it('⭐ 开关是**真 layui switch**：打开面板时按 `lay-filter` 调了一次 `form.render(checkbox, …)`', async () => {
    await mount()
    // 挂载时**不**渲染（面板还没开）。
    expect(LAYUI.formRenders).toHaveLength(0)
    openCallbackPanel(APP_A)
    // ⚠️ 少了这一句，页面上那个 `lay-skin="switch"` 就只是**原生复选框**（layui 不认裸 input），
    //    而这在 `typecheck` / `lint` / 别的用例里**都看不见**（静默退化）。
    // ⚠️ 能红：把 `openCallback` 里的 `success: () => renderCallbackForm()` 删掉 ⇒ 本断言红。
    expect(LAYUI.formRenders).toEqual([{ type: 'checkbox', filter: CALLBACK_FORM_FILTER }])
    // ⛔ 用的是**带 `lay-filter` 的那一形**，⛔ 不是无参的全局渲染（那会重渲页面上别的表单）。
    expect(LAYUI.formRenders[0]!.filter).not.toBeUndefined()
  })

  it('⭐ 回调块套在 `.layui-form[lay-filter]` 里 + 地址上限 = 后端列宽（⛔ 不自己编一个数）', () => {
    // layui 的 `form.render(type, filter)` 根选择器就是 `.layui-form[lay-filter=…]`（读源码实测）
    // ⇒ 少了这一层，标出来的 `lay-skin="switch"` 就是**一句空话**（页面上还是原生复选框）。
    const d = new DOMParser().parseFromString(
      `<div id="root">${buildHtml(true)}</div>`,
      'text/html',
    )
    const box = d.getElementById(CALLBACK_STATE_SWITCH_ID)!
    expect(box.closest('.layui-form')?.getAttribute('lay-filter')).toBe(CALLBACK_FORM_FILTER)
    expect(box.getAttribute('lay-skin')).toBe('switch')
    expect(box.getAttribute('name')).toBe(CALLBACK_STATE_NAME)
    const url = d.getElementById(CALLBACK_URL_INPUT_ID)!
    expect(url.closest('.layui-form')).not.toBeNull()
    // ⚠️ 上限与后端 `CALLBACK_URL_MAX_LENGTH`（= `TakerApp.callback_url` 的列宽）**同值**；
    //    这里断言的是"标记里的数与那个常量一致"（⛔ 两侧都不许自己拍一个）。
    expect(url.getAttribute('maxlength')).toBe(String(CALLBACK_URL_MAX_LENGTH))
    expect(CALLBACK_URL_MAX_LENGTH).toBe(200)
  })

  it('`callbackUrlHint`：空 / 合法 https ⇒ 无提示；其余 ⇒ 提示（空是**合法值**）', () => {
    expect(callbackUrlHint('')).toBe('')
    expect(callbackUrlHint('   ')).toBe('')
    expect(callbackUrlHint('https://hooks.example.com/tt')).toBe('')
    expect(callbackUrlHint('http://hooks.example.com/tt')).toBe(CALLBACK_URL_HINT)
    expect(callbackUrlHint('https://')).toBe(CALLBACK_URL_HINT)
    // ⚠️ 那句提示必须说清"https + 公网"（T6 逐字要求）。
    expect(CALLBACK_URL_HINT).toContain('https')
    expect(CALLBACK_URL_HINT).toContain('公网')
  })

  it('`callbackSaveReport`：三个分支各自**说破后果**（空 / 停用 ⇒ 一条都不会推）', () => {
    expect(callbackSaveReport('', 'enabled')).toContain('不会推送')
    expect(callbackSaveReport('https://a.example.com/x', 'disabled')).toContain('不会推送')
    const on = callbackSaveReport('https://a.example.com/x', 'enabled')
    expect(on).toContain('查看推送台账')
    expect(on).not.toContain('不会推送')
  })

  it('`deliveriesHtml`（**与上家侧同一份实现**）：空列表说"还没有"；字符串转义；重投按钮只在 failed 行', () => {
    // 空列表 vs 取不到是两件事：这里只说"还没有"（取不到由调用方写进 warn 容器）。
    expect(deliveriesHtml([], 0)).toContain('还没有推送记录')
    // 来自后端的字符串一律转义（`reason` 是**对方响应里的原文**，最脏的就是它）。
    const dirty = deliveriesHtml(
      [{ ...TAKER_DELIVERY_FAILED, reason: '<img src=x onerror=1>' }],
      1,
    )
    expect(dirty).not.toContain('<img')
    // ⭐ 恰好一个重投按钮，且挂在 failed 那一行上。
    const html = deliveriesHtml([TAKER_DELIVERY_DONE, TAKER_DELIVERY_FAILED], 2)
    expect([...html.matchAll(new RegExp(DELIVERY_RETRY_ATTR, 'g'))]).toHaveLength(1)
    expect(html).toContain(`${DELIVERY_RETRY_ATTR}="${TAKER_DELIVERY_FAILED.id}"`)
  })

  // ── 纯函数（各自一处判据）─────────────────────────────────────────

  it('`stateActionLabel` / `stateCell`：判据用**机器值** `state`，⛔ 不用中文名', () => {
    expect(stateActionLabel(APP_A)).toBe('停用')
    expect(stateActionLabel(APP_B)).toBe('启用')
    expect(stateCell(APP_A)).toContain('启用')
    expect(stateCell(APP_B)).toContain('停用')
  })

  it('`timeText` / `lastUsedText`：null 与空串**说人话**（⛔ 不是空白格）', () => {
    expect(timeText(null)).toBe('—')
    expect(timeText('')).toBe('—')
    expect(timeText('2026-10-08T10:00:00+08:00')).toBe('2026-10-08 10:00:00')
    expect(lastUsedText(null)).toBe('—（还没被调用过）')
  })

  it('`secretCell`：明文上屏且转义（⛔ 不假设密钥的字符集）', () => {
    expect(secretCell(APP_A)).toContain(APP_A.app_secret)
    expect(secretCell({ ...APP_A, app_secret: '<img src=x>' })).not.toContain('<img')
  })

  it('`candidateOptionsHtml` / `pickerHintText`：第一项是空值占位、文字转义、三档提示', () => {
    const html = candidateOptionsHtml([{ id: 7, username: 'a<b', display_name: '打手<甲>' }])
    expect(html.startsWith('<option value="">')).toBe(true)
    expect(html).toContain('value="7"')
    expect(html).not.toContain('<b>')
    expect(candidateLabel({ username: 'u1', display_name: '' })).toBe('u1（u1）')
    expect(pickerHintText(0, 0)).toBe(MESSAGES.pickerEmpty)
    expect(pickerHintText(2, 500)).toContain('500')
    expect(pickerHintText(2, 2)).toBe('')
  })
  // ── ⭐ 「接入文档」入口────────────────────────────────
  //
  // 出处是仓库根的 `docs/TAKER-API.md`；页面读的是它在 Vite root 之内的**逐字节副本**
  // `src/docs/taker-api.md`（为什么必须有副本，见文件头与 `src/docs/AGENTS.md`）。
  // ⇒ 判据分两路：① **两份文件逐字节相同**（下面那条网）；② 页面渲染 / 复制 / 下载出来的内容
  // 与**仓库根那份**对得上（⛔ 不是"拿页面的输出比它自己"）。

  /** 出处的绝对路径：仓库根的 `docs/TAKER-API.md`（唯一权威）。 */
  const DOC_PATH = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../../../docs/TAKER-API.md',
  )
  /** 页面真正 import 的那份副本。 */
  const DOC_COPY_PATH = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../docs/taker-api.md',
  )
  /** 文件此刻的内容（每次现读：弄红证明会临时改文件，读到旧内容就成了假绿）。 */
  function docFile(): string {
    return fs.readFileSync(DOC_PATH, 'utf8')
  }

  /** 打开「接入文档」弹层（工具栏那颗按钮），并返回两个滚动容器。 */
  function openDocPanel(): { toc: HTMLElement; body: HTMLElement } {
    toolbar(DOC_TOOLBAR_EVENT)
    return { toc: at<HTMLElement>(DOC_TOC_ID), body: at<HTMLElement>(DOC_BODY_ID) }
  }

  it('⛔ 副本网：`src/docs/taker-api.md` 与 `docs/TAKER-API.md` **逐字节相同**（漂了就红）', () => {
    // ⚠️ 这条网是**退路写法**的代价所在（`AGENTS.md` §5-C / E-0011 的同族）：
    //    「两份都改还不报错」正是要堵的形态 ⇒ 判据只能是"一模一样"。
    // ⛔ 不许放宽成 `toContain` / 比长度 / 只比前 N 行。
    const original = docFile()
    const copy = fs.readFileSync(DOC_COPY_PATH, 'utf8')
    // 先自证"读到了真东西"（否则空 == 空 也是绿的）。
    expect(original.length).toBeGreaterThan(1000)
    expect(original).toContain('团团通**打手侧**开放接口')
    expect(
      copy,
      '两份漂了：请在 `src/frontend/` 下重跑 `cp ../../docs/TAKER-API.md src/docs/taker-api.md`',
    ).toEqual(original)
  })

  it('⭐ 驱动真交互：点工具栏「接入文档」⇒ 弹层真的打开（DOM 里出现容器 + 目录 + 正文）', async () => {
    await mount()
    // ① 标记里有那颗按钮（工具栏模板是 `buildHtml` 的一部分）。
    expect(buildHtml(true)).toContain(`lay-event="${DOC_TOOLBAR_EVENT}"`)
    expect(root.querySelector(DOC_SELECTOR)).not.toBeNull()
    // ② 点**之前**：目录与正文都是空的（下面"点之后有内容"才说明是这一次点击干的活）。
    expect(at<HTMLElement>(DOC_TOC_ID).innerHTML).toBe('')
    expect(at<HTMLElement>(DOC_BODY_ID).innerHTML).toBe('')

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
    // ⭐ 内容非空：正文里有真文档的原话（⛔ 不是空壳）。
    const text = body.textContent ?? ''
    expect(text).toContain('接入三步')
    expect(text).toContain('只能按订单号搜')
    // ⑥ ⛔ 弹层里**没有** `<script>`：只插 HTML，不给文档内容执行脚本的机会。
    expect(root.querySelector(DOC_SELECTOR)!.querySelectorAll('script')).toHaveLength(0)
  })

  it('⭐ 目录：`##` 一级条目数 == 文档里 `## ` 的行数；`###` 也在，且含已知标题字面量', async () => {
    await mount()
    const { toc } = openDocPanel()
    const raw = docFile()
    // ⚠️ 判据的**左侧**是 fs 读到的原文行数、**右侧**是 `marked` 产物里派生的目录（两路来源）。
    const h2Count = raw.split('\n').filter((line) => line.startsWith('## ')).length
    const h3Count = raw.split('\n').filter((line) => line.startsWith('### ')).length
    expect(h2Count).toBeGreaterThanOrEqual(10)
    expect(toc.querySelectorAll('[data-doc-depth="2"]')).toHaveLength(h2Count)
    expect(toc.querySelectorAll('[data-doc-depth="3"]')).toHaveLength(h3Count)
    const text = toc.textContent ?? ''
    expect(text).toContain('0. 三十秒总览（先读这一段）')
    expect(text).toContain('1. 接入三步')
    // ⚠️ 标题里的 markdown 强调**不该**在目录里露出星号（走 `htmlToText` 去过标签）。
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
    const second = linkOf('11. 给 AI 的排查顺序')

    // ① 目录项的锚点**就是**正文里那个标题的 `id`（否则点了跳不过去）。
    const headingOf = (link: HTMLElement): HTMLElement => {
      const anchor = link.getAttribute(DOC_ANCHOR_ATTR)!
      const heading = document.getElementById(anchor)
      if (!heading) throw new Error(`正文里没有锚点 ${anchor}`)
      return heading
    }
    expect(headingOf(first).textContent).toContain('接入三步')
    expect(headingOf(second).textContent).toContain('排查顺序')
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
    expect(root.querySelector(DOC_SELECTOR)!.getAttribute('style')).toContain('display:none')
  })

  it('⭐ 内容来自**真文档**：渲染结果里有 docs/TAKER-API.md 的已知原话', () => {
    const text = (takerApiDoc().html ?? '')
      .replace(/<[^>]*>/g, '')
      .replace(/&(amp|lt|gt|quot|apos|nbsp);/g, ' ')
      .replace(/\s+/g, ' ')
    expect(text).toContain('把厅里的单')
    expect(text).toContain('必须在服务端调')
    // 对照：那句话**确实**来自这个文件（⛔ 不是页面里另抄了一份）。
    const raw = docFile()
    expect(raw).toContain('把厅里的单')
    expect(raw).toContain('必须在服务端调')
  })

  it('⭐ 尖括号占位符在渲染结果里**还在**（判据：先解实体、再去标签，再在纯文本里找）', () => {
    const html = takerApiDoc().html
    // ① 前提自证：这些占位符在**文档原文**里就有（否则下面的判据是空跑）。
    const raw = docFile()
    for (const ph of ['<order_no>', '<令牌>']) {
      expect(raw).toContain(ph)
    }
    // ② 渲染器把它们**转义**了 ⇒ 直接在 HTML 字符串里搜字面量是**恒假**的。
    expect(html).toContain('&lt;order_no&gt;')
    expect(html).not.toContain('<order_no>')
    // ③ ⭐ 正判据：**先解实体、再去标签**，在纯文本里找占位符本身。
    const text = html
      .replace(/<[^>]*>/g, '')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&')
      .replace(/&quot;/g, '"')
    for (const ph of ['<order_no>', '<令牌>']) {
      expect(text).toContain(ph)
    }
  })

  it('⭐ 白名单标签网：标签名 ⊆ 白名单；且没有事件处理属性 / `javascript:` 协议', () => {
    const html = takerApiDoc().html
    const names = [...html.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9-]*)/g)].map((m) =>
      m[1]!.toLowerCase(),
    )
    // ⚠️ 先自证"扫到了东西"（否则空集合 ⊆ 白名单是恒真的）。
    expect(names.length).toBeGreaterThan(100)
    expect(new Set(names).size).toBeGreaterThan(10)
    const ALLOWED_TAGS = new Set([
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'p', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'hr', 'br', 'strong', 'em', 'del', 'a', 'img',
    ])
    const outside = [...new Set(names)].filter((name) => !ALLOWED_TAGS.has(name))
    expect(
      outside,
      `渲染结果里出现了白名单之外的标签：${outside.join(', ')}——文档里写了裸 HTML？`,
    ).toEqual([])
    // ⚠️ 标签名在白名单里**不等于**安全：`<img onerror=…>` 的标签名是 `img`。
    expect([...html.matchAll(/\son[a-z]+\s*=/gi)].map((m) => m[0])).toEqual([])
    expect(html).not.toMatch(/(?:href|src)\s*=\s*["']?\s*javascript:/i)
    // 对照：白名单里**确实**有用到的标签。
    expect(new Set(names)).toContain('table')
    expect(new Set(names)).toContain('code')
  })

  it('⭐ 「复制 Markdown 原文」拿到的字符串 == docs/TAKER-API.md 的文件内容（逐字节）', async () => {
    await mount()
    const writeText = vi.fn((_text: string) => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    openDocPanel()
    at<HTMLButtonElement>(DOC_COPY_BUTTON_ID).click()
    await flush()
    expect(writeText).toHaveBeenCalledTimes(1)
    // ⚠️ 判据的两侧是**两路来源**：动作真正写给剪贴板的那个串 vs `fs` 读同一个文件。
    expect(writeText.mock.calls[0]![0]).toBe(docFile())
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(DOC_COPY_DONE)
  })

  it('⭐ 复制文档失败（浏览器不给剪贴板）**说出来并给出路**，⛔ 不静默假装成功', async () => {
    await mount()
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    openDocPanel()
    at<HTMLButtonElement>(DOC_COPY_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(DOC_COPY_FAILED)
    // ⚠️ 提示里要**给一条出路**（那颗「下载 .md」），⛔ 不是"复制失败"四个字了事。
    expect(DOC_COPY_FAILED).toContain('下载 .md')
  })

  it('⭐ 「下载 .md」：Blob + `a[download]`，存下来的就是那份文件；做不到时说人话', async () => {
    await mount()
    openDocPanel()
    // ⚠️ 替身要**声明一个形参**：写成 `vi.fn(() => '…')` 时 `mock.calls` 的元素类型是空元组
    //    `[]` ⇒ 下面取 `calls[0][0]` 会被 `vue-tsc` 报 TS2493。
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:synthetic-taker-doc')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
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
      expect(clicked[0]!.href).toBe('blob:synthetic-taker-doc')
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:synthetic-taker-doc')
    } finally {
      clickSpy.mockRestore()
    }

    // ② 降级分支：这个环境**存不了文件** ⇒ 必须说人话并给出路（⛔ 不是点了没反应）。
    LAYUI.messages.length = 0
    Object.defineProperty(URL, 'createObjectURL', { value: undefined, configurable: true })
    at<HTMLButtonElement>(DOC_DOWNLOAD_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(DOC_DOWNLOAD_FAILED)
    expect(DOC_DOWNLOAD_FAILED).toContain('docs/TAKER-API.md')
  })

  it('⭐ 弹层反复开关**不重跑渲染**：第二次打开目录 / 正文与第一次逐字相同', async () => {
    await mount()
    const { toc, body } = openDocPanel()
    const firstToc = toc.innerHTML
    const firstBody = body.innerHTML
    expect(firstBody.length).toBeGreaterThan(1000)
    // 关掉（`end` 回调）再开一次：`layer.open` 的替身不动节点，`end` 也没被调 ⇒ 手动收一次。
    ;((LAYUI.opens[0] as { end: () => void }).end)()
    openDocPanel()
    expect(LAYUI.opens).toHaveLength(2)
    expect(toc.innerHTML).toBe(firstToc)
    expect(body.innerHTML).toBe(firstBody)
    // 关掉弹层后节点被收回页面（否则第二次打开取不到节点、弹层静默不出现）。
    expect(root.querySelector(DOC_SELECTOR)).not.toBeNull()
  })

  it('⭐ `takerApiDocText()` 就是那份文件的原文（逐字节，且非空）', () => {
    // ⚠️ 判据的两侧是**两路来源**：页面拿到的原文 vs `fs` 读同一个文件。
    const text = takerApiDocText()
    expect(text.length).toBeGreaterThan(1000)
    expect(text).toBe(docFile())
  })

  it('⭐ 文档弹层的提示说清"内容是仓库里那份文件、改了要重新构建"', async () => {
    await mount()
    expect(root.innerHTML).toContain(DOC_PANEL_NOTICE)
    expect(DOC_PANEL_NOTICE).toContain('docs/TAKER-API.md')
    expect(DOC_PANEL_NOTICE).toContain('重新构建')
    // ⛔ 页面上的中文提示里**不许留 markdown 的 `**`**（走 `innerHTML`，浏览器不认 markdown）。
    expect(DOC_PANEL_NOTICE).not.toContain('**')
  })
})
