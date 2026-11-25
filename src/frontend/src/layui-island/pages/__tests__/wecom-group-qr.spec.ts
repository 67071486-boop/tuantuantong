// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import type { WecomQrAppRow, WecomQrDeliveryRow } from '@/api/wecomGroupQr'

/**
 * 【第三方平台 → 自动上传企微群码到订单截图】island 页的契约测试（2026-10-09，企微群码线 T6）。
 *
 * 这一页是**自有功能**（对标物里没有【第三方平台】这一组），所以没有证据件可对拍；本文件钉的是
 * **有真实后果**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    **创建 → 显示密钥 → 复制 → 保存回调 → 轮换**一整条链，外加 IP 白名单、停用启用、
 *    台账折叠与拉取失败两条分支。
 * 2. **四条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量、以 `(` 开头的语句前置分号、
 *    先挂样式 → 再塞标记 → 最后 `initIslandElements`（以**运行期 DOM** 判断，不是读源码）、
 *    页里有定时器时假定时器要先于 `setInterval` 包装且用例自己收口
 *    （⚠️ 本页**没有**定时器，所以这一条在这里体现为「各用例自己 `document.body.innerHTML = ''` 收口」）。
 * 3. ⭐ **页内自检那道门**（设计文档 **D10**）：非账号管理员（哪怕**持有**那个节点）深链打开 ⇒
 *    `msg` 一句无权 + **不注入标记** + **一个请求都不发**（fail-closed）。
 *    ⚠️ 这条用例就是本任务那条**变异**的目标：把页里那道自检去掉 / 改成恒放行，它必须红。
 * 4. ⭐ **「每租户只有这一把」在页面上说破**（业务方 A6 的原话「不能设置多个」）。
 * 5. ⛔ **页面上没有**的东西（判据打在**标记**与**运行时导出**上，⛔ 不是"源码文件文本里没有这个词"，
 *    见 `AGENTS.md` §5-G / E-0161）：权限设置 / 游戏范围（那是「应用管理」页已删的东西）、
 *    删除按钮（沿用"只能停用不能删"）、名称输入框（`name` 不在该端点 PATCH 的可改字段里）、
 *    「只显示这一次」那类与实现相反的措辞（密钥**随时可取**）。
 *
 * ## 手法（照 `__tests__/open-apps.spec.ts`，⛔ 不自己发明一套）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `layer.*` 的**入参**
 *   才能被断言（本页没有 `table`，所以替身里也没有它——缺了它页面照样挂载，见页里的降级分支）；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/wecomGroupQr` 跑的是**真实现** ⇒
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义（把 api 模块也换成替身的话，
 *   那只是在断言自己传了一个对象）；
 * - `vi.mock('@/api/auth')`：本页第一句就 `fetchMe()`（自检那道门）⇒ 必须可控。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：应用名 / 密钥 / 单号都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const opens: Array<Record<string, unknown>> = []
  const confirms: string[] = []
  const confirmOks: Array<() => void> = []
  const formRenders: Array<{ type?: string; filter?: string }> = []
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    opens,
    confirms,
    confirmOks,
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
      confirm: vi.fn((text: string, _o: unknown, ok: () => void) => {
        confirms.push(text)
        confirmOks.push(ok)
      }),
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
    LAYUI.seenByBootstrap.push(document.querySelector('#wecomQrCreateBtn') !== null)
    return { layer: LAYUI.layer, form: LAYUI.form }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#wecomQrCreateBtn') !== null)
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
  APP_SELECTOR,
  CALLBACK_FORM_FILTER,
  CALLBACK_HINT_ID,
  CALLBACK_SAVE_BUTTON_ID,
  CALLBACK_STATE_SWITCH_ID,
  CALLBACK_URL_HINT,
  CALLBACK_URL_INPUT_ID,
  CARD_SELECTOR,
  CAPABILITY_ID,
  CAPABILITY_NOTICE,
  COPY_SECRET_BUTTON_ID,
  CREATE_BUTTON_ID,
  DELIVERIES_BODY_ID,
  DELIVERIES_TOGGLE_CLOSED,
  DELIVERIES_TOGGLE_ID,
  DELIVERIES_TOGGLE_OPEN,
  DELIVERIES_WARN_ID,
  DOMAIN_TEXT_ID,
  EMPTY_SELECTOR,
  IP_INPUT_ID,
  IP_SAVE_BUTTON_ID,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  ONLY_ONE_NOTICE,
  ROTATE_BUTTON_ID,
  SECRET_TEXT_ID,
  STATUS_TEXT_ID,
  TICKET_EXAMPLE,
  TOGGLE_BUTTON_ID,
  __testables,
} from '../wecom-group-qr'

const {
  buildHtml,
  callbackSaveReport,
  callbackUrlHint,
  copyTextToClipboard,
  credentialText,
  deliveriesHtml,
  ipSaveReport,
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

/** 本租户那把应用：启用、已配回调（合法 https 地址）、白名单为空。 */
const APP: WecomQrAppRow = {
  id: 1,
  name: '自动上传企微群码',
  app_key: '0123456789abcdef',
  // ⭐ 密钥**就在行载荷里**（`app_row_payload` 逐字复用）⇒ 页面直接显示明文并给复制动作。
  app_secret: 'SYNTHETIC-secret-W1x2Y3z4',
  state: 'enabled',
  state_text: '启用',
  ip_allowlist: '',
  callback_url: 'https://callback.example.com/tt',
  callback_state: 'enabled',
  last_used_at: null,
  secret_rotated_at: null,
  created_at: '2026-10-09T10:00:00+08:00',
}

/** 创建那一次拿到的（还没配回调 ⇒ 地址空 + 开关停用）。 */
const CREATED: WecomQrAppRow = {
  ...APP,
  app_secret: 'SYNTHETIC-secret-CREATED-A9b8C7',
  callback_url: '',
  callback_state: 'disabled',
}

/** 轮换之后的行（密钥换掉，其余不动）。 */
const ROTATED: WecomQrAppRow = { ...APP, app_secret: 'SYNTHETIC-secret-ROTATED-Z9y8X7' }

/** 台账三行（**三种状态各一条**，判据要能分辨它们）。本页**不画**任何按状态分叉的动作。 */
const DELIVERY_FAILED: WecomQrDeliveryRow = {
  id: 31,
  event: 'order.status_changed',
  event_text: '订单状态变化',
  order_no: 'TT202610090001',
  source_order_no: '54556512310200',
  status: 'failed',
  status_text: '投递失败',
  attempts: 7,
  reason: '对方回执不是 2xx',
  response_status: 500,
  next_attempt_at: null,
  created_at: '2026-10-09T17:20:00+08:00',
  processed_at: '2026-10-09T17:30:00+08:00',
}

const DELIVERY_DONE: WecomQrDeliveryRow = {
  id: 32,
  event: 'order.image_added',
  event_text: '订单新增图片',
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

const DELIVERY_PENDING: WecomQrDeliveryRow = {
  id: 33,
  event: 'order.deleted',
  event_text: '订单删除',
  order_no: 'TT202610090003',
  source_order_no: '',
  status: 'pending',
  status_text: '待投递',
  attempts: 1,
  reason: '连接超时',
  response_status: null,
  next_attempt_at: '2026-10-09T17:31:00+08:00',
  created_at: '2026-10-09T17:30:00+08:00',
  processed_at: '2026-10-09T17:30:05+08:00',
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

describe('自动上传企微群码到订单截图 island 页（wecom-group-qr）', () => {
  let root: HTMLElement
  /** 服务端**此刻**那把应用（`null` = 还没建；创建 / PATCH / 轮换会改它）。 */
  let appOnServer: WecomQrAppRow | null
  /** 服务端**此刻**那份台账。 */
  let deliveriesOnServer: typeof DELIVERY_PAGE

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
    LAYUI.opens.length = 0
    LAYUI.confirms.length = 0
    LAYUI.confirmOks.length = 0
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
      menu_permissions: ['system.pinduoduo_settings'],
      permissions: [],
    })

    routes = {}
    // ⚠️ **响应是"活的"**：创建 / PATCH / 轮换之后重新取必须**看得到新值**
    //    （否则那些判据只是在断言**夹具**，而不是页面的行为）。
    appOnServer = APP
    deliveriesOnServer = DELIVERY_PAGE
    ready('GET /api/open/wecom-qr-app/', () => appOnServer)
    ready('POST /api/open/wecom-qr-app/', () => {
      appOnServer = CREATED
      return CREATED
    })
    // PATCH 把入参**并回**那一行（PATCH 语义：缺席的键保持库里的值）⇒ 刷新后看得到改动。
    ready('PATCH /api/open/wecom-qr-app/', (data) => {
      if (appOnServer === null) throw new Error('还没创建就 PATCH 了')
      appOnServer = { ...appOnServer, ...((data ?? {}) as Partial<WecomQrAppRow>) }
      return appOnServer
    })
    ready('POST /api/open/wecom-qr-app/rotate-secret/', () => {
      appOnServer = ROTATED
      return ROTATED
    })
    ready('GET /api/open/wecom-qr-app/deliveries/', () => deliveriesOnServer)
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

  /** 页面上某个节点（本页没有弹层 ⇒ 节点都在 root 里）。 */
  function at<T extends HTMLElement>(id: string): T {
    const el = root.querySelector<T>(`#${id}`)
    if (!el) throw new Error(`页面上没有 #${id}`)
    return el
  }

  /** 装一个可控剪贴板，返回它的 `writeText` 替身。 */
  function stubClipboard(): ReturnType<typeof vi.fn> {
    const writeText = vi.fn((_text: string) => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    return writeText
  }

  // ── 挂载顺序（陷阱 ③，运行期判据）────────────────────────────

  it('先挂样式再塞标记：bootstrapIsland 看不到标记、initIslandElements 看得到', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('类名与选择器是两个常量：标记里是 `class="layui-card wecomQrCard"`（⛔ 不是 `.wecomQrCard`）', async () => {
    await mount()
    // 写成 `class="${CARD_SELECTOR}"` 会造出 `class=".wecomQrCard"`、永远匹配不到。
    expect(root.innerHTML).not.toContain('class=".wecomQrCard"')
    // 六个区块（未创建 / 接入信息 / 回调 / 白名单 / 台账 / 能力说明）都在 —— 少一个就是"点了没反应"。
    expect(root.querySelectorAll(CARD_SELECTOR)).toHaveLength(6)
    expect(root.querySelector(EMPTY_SELECTOR)).not.toBeNull()
    expect(root.querySelector(APP_SELECTOR)).not.toBeNull()
    for (const id of [
      CREATE_BUTTON_ID,
      DOMAIN_TEXT_ID,
      SECRET_TEXT_ID,
      COPY_SECRET_BUTTON_ID,
      CALLBACK_URL_INPUT_ID,
      CALLBACK_STATE_SWITCH_ID,
      CALLBACK_SAVE_BUTTON_ID,
      IP_INPUT_ID,
      IP_SAVE_BUTTON_ID,
      DELIVERIES_TOGGLE_ID,
      DELIVERIES_BODY_ID,
      CAPABILITY_ID,
    ]) {
      expect(root.querySelector(`#${id}`), `标记里缺 #${id}`).not.toBeNull()
    }
  })

  it('开关按 layui 的用法渲染：`form.render("checkbox", <filter>)` 被调到（否则是原生复选框）', async () => {
    await mount()
    expect(LAYUI.formRenders.length).toBeGreaterThan(0)
    expect(LAYUI.formRenders[0]).toEqual({ type: 'checkbox', filter: CALLBACK_FORM_FILTER })
  })

  // ── 未创建那一支 ────────────────────────────────────────────

  it('还没创建 ⇒ 显示「创建」那块、藏起「已创建」那几块；GET 恰好一次', async () => {
    appOnServer = null
    await mount()
    expect(at(CREATE_BUTTON_ID)).toBeTruthy()
    expect(root.querySelector<HTMLElement>(EMPTY_SELECTOR)!.style.display).toBe('')
    expect(root.querySelector<HTMLElement>(APP_SELECTOR)!.style.display).toBe('none')
    expect(callCount('get', '/api/open/wecom-qr-app/')).toBe(1)
    // 还没建就不该有任何写请求。
    expect(mockedRequest.mock.calls.map((c) => (c[0] as Cfg).method)).toEqual(['get'])
    // 「每租户只有这一把」这句**必须在页面上说破**（业务方 A6 的原话）。
    expect(root.textContent).toContain(ONLY_ONE_NOTICE)
  })

  it('已创建 ⇒ 反过来：创建块收起、接入信息那几块显示，域名取当前浏览器地址', async () => {
    await mount()
    expect(root.querySelector<HTMLElement>(EMPTY_SELECTOR)!.style.display).toBe('none')
    expect(root.querySelector<HTMLElement>(APP_SELECTOR)!.style.display).toBe('')
    expect(at(DOMAIN_TEXT_ID).textContent).toBe(window.location.origin)
    expect(at<HTMLInputElement>(SECRET_TEXT_ID).value).toBe(APP.app_secret)
    expect(at(STATUS_TEXT_ID).textContent).toBe(APP.state_text)
    expect(at<HTMLButtonElement>(TOGGLE_BUTTON_ID).textContent).toBe('停用')
  })

  // ── ⭐ 一整条链：创建 → 显示密钥 → 复制 → 保存回调 → 轮换 ──────

  it('⭐ 创建 → 显示密钥 → 复制 → 保存回调 → 轮换（**一条链**）', async () => {
    appOnServer = null
    await mount()
    const writeText = stubClipboard()
    expect(callCount('get', '/api/open/wecom-qr-app/')).toBe(1)

    // ① 点「创建应用」⇒ 真发 POST，body 是 `{}`（名字由后端给，⛔ 不是前端编一个）。
    at<HTMLButtonElement>(CREATE_BUTTON_ID).click()
    await flush()
    expect(callTo('post', '/api/open/wecom-qr-app/').data).toEqual({})
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('已创建')

    // ② 建完密钥**当场显示**（明文、可随时复制）；创建块收起。
    expect(callCount('get', '/api/open/wecom-qr-app/')).toBe(2)
    expect(at<HTMLInputElement>(SECRET_TEXT_ID).value).toBe(CREATED.app_secret)
    expect(root.querySelector<HTMLElement>(EMPTY_SELECTOR)!.style.display).toBe('none')

    // ③ 复制：剪贴板拿到的是 AppKey + AppSecret **两行**（从行数据取，⛔ 不从 DOM 拼）。
    at<HTMLButtonElement>(COPY_SECRET_BUTTON_ID).click()
    await flush()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0]![0]).toBe(credentialText(CREATED))
    expect(writeText.mock.calls[0]![0]).toContain(CREATED.app_key)
    expect(writeText.mock.calls[0]![0]).toContain(CREATED.app_secret)

    // ④ 填回调地址 + 打开开关（派发真 `input` / 改 `checked`），再保存。
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = 'https://callback.example.com/tt'
    input.dispatchEvent(new Event('input'))
    // 合法 https ⇒ 即时提示清空（它不是判据，但"边打边说"是这一块的可见反馈）。
    expect(at(CALLBACK_HINT_ID).textContent).toBe('')
    at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID).checked = true
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()
    // ⑤ PATCH 的 body 是**两个键一起**（地址与开关是同一份配置）。
    expect(callTo('patch', '/api/open/wecom-qr-app/').data).toEqual({
      callback_url: 'https://callback.example.com/tt',
      callback_state: 'enabled',
    })
    // ⑥ 回报**说破后果**（⛔ 不是"保存成功"四个字了事）。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(
      callbackSaveReport('https://callback.example.com/tt', 'enabled'),
    )
    // ⑦ 刷新之后页面上那份数据是新值。
    expect(at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value).toBe(
      'https://callback.example.com/tt',
    )

    // ⑧ 轮换：二次确认（里面必须有"旧密钥立刻作废"）→ POST → 新密钥当场显示。
    at<HTMLButtonElement>(ROTATE_BUTTON_ID).click()
    expect(LAYUI.confirms).toHaveLength(1)
    expect(LAYUI.confirms[0]).toContain('旧密钥立刻作废')
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('post', '/api/open/wecom-qr-app/rotate-secret/')).toBeTruthy()
    expect(at<HTMLInputElement>(SECRET_TEXT_ID).value).toBe(ROTATED.app_secret)
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('旧密钥当场作废')
  })

  it('创建失败（已有一把 ⇒ 后端 1000）⇒ **如实显示后端文案**，⛔ 不换成笼统的"操作失败"', async () => {
    appOnServer = null
    await mount()
    ready('POST /api/open/wecom-qr-app/', () => {
      throw new BizError(1000, '本租户已创建过该应用，不能重复创建')
    })
    at<HTMLButtonElement>(CREATE_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe('本租户已创建过该应用，不能重复创建')
  })

  it('⭐ 复制失败（浏览器不给剪贴板）**说出来**，⛔ 不静默假装成功', async () => {
    await mount()
    // 先证"接上了"：jsdom 里 `navigator.clipboard` 默认**不存在** ⇒ 走失败那一支。
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    at<HTMLButtonElement>(COPY_SECRET_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.copyFailed)
    // 换个"存在但被拒"的替身 ⇒ 同样要报失败（⛔ 不是只在"没有 clipboard"时才报）。
    LAYUI.messages.length = 0
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: () => Promise.reject(new Error('NotAllowedError')) },
      configurable: true,
    })
    at<HTMLButtonElement>(COPY_SECRET_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.copyFailed)
  })

  // ── 回调地址：即时提示（前端）+ 判据以后端为准 ────────────────

  it('⭐ 回调地址：非 https ⇒ 当场提示；https 或空 ⇒ 不提示（空是**合法值**）', async () => {
    await mount()
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    const hint = (): string => at(CALLBACK_HINT_ID).textContent ?? ''
    // 打开时这一行自己就是合法 https ⇒ 没有提示。
    expect(hint()).toBe('')
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
  })

  it('⭐ 判据以后端为准：非法地址后端回 1000 ⇒ 页面把后端那句**显示出来**', async () => {
    await mount()
    ready('PATCH /api/open/wecom-qr-app/', () => {
      throw new BizError(1000, '回调地址只支持 https')
    })
    at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value = 'http://callback.example.com/tt'
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe('回调地址只支持 https')
  })

  it('保存回调：地址清空 + 关开关 ⇒ PATCH 两个键都在，回报说破"不会推送"', async () => {
    await mount()
    const input = at<HTMLInputElement>(CALLBACK_URL_INPUT_ID)
    input.value = ''
    input.dispatchEvent(new Event('input'))
    at<HTMLInputElement>(CALLBACK_STATE_SWITCH_ID).checked = false
    at<HTMLButtonElement>(CALLBACK_SAVE_BUTTON_ID).click()
    await flush()
    expect(callTo('patch', '/api/open/wecom-qr-app/').data).toEqual({
      callback_url: '',
      callback_state: 'disabled',
    })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('不会推送')
    expect(at<HTMLInputElement>(CALLBACK_URL_INPUT_ID).value).toBe('')
  })

  // ── IP 白名单（**空串是合法值**：判断键在不在，⛔ 不是真值，E-0021）────

  it('⭐ IP 白名单：清空也要**发出这个键**（`{ip_allowlist: ""}`），⛔ 不许当成"没填"静默不发', async () => {
    await mount()
    // 先设一个值：保存之后页面上看到的是它。
    const ip = at<HTMLTextAreaElement>(IP_INPUT_ID)
    ip.value = '203.0.113.9'
    at<HTMLButtonElement>(IP_SAVE_BUTTON_ID).click()
    await flush()
    expect(callTo('patch', '/api/open/wecom-qr-app/').data).toEqual({ ip_allowlist: '203.0.113.9' })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('只有白名单里的 IP')

    // 清空（空是**合法值** = 不限制）⇒ 这个键**照样要发**。
    at<HTMLTextAreaElement>(IP_INPUT_ID).value = ''
    at<HTMLButtonElement>(IP_SAVE_BUTTON_ID).click()
    await flush()
    expect(callTo('patch', '/api/open/wecom-qr-app/').data).toEqual({ ip_allowlist: '' })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toContain('不限制来源 IP')
  })

  // ── 推送台账（折叠面板）──────────────────────────────────────

  it('「最近推送记录」是可折叠面板：默认收起、点一下展开并取一次、再点收起（**真交互**）', async () => {
    await mount()
    const body = at(DELIVERIES_BODY_ID)
    expect(body.style.display).toBe('none')
    expect(at(DELIVERIES_TOGGLE_ID).textContent).toBe(DELIVERIES_TOGGLE_CLOSED)
    expect(callCount('get', '/api/open/wecom-qr-app/deliveries/')).toBe(0)

    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    expect(at(DELIVERIES_BODY_ID).style.display).toBe('')
    expect(at(DELIVERIES_TOGGLE_ID).textContent).toBe(DELIVERIES_TOGGLE_OPEN)
    expect(callCount('get', '/api/open/wecom-qr-app/deliveries/')).toBe(1)
    // 三行都在，六列的表头也在（本页**没有**"操作"那一列 —— §7.3 里没有重投端点）。
    const html = at(DELIVERIES_BODY_ID).innerHTML
    expect(html).toContain('投递失败')
    expect(html).toContain('已投递')
    expect(html).toContain('待投递')
    expect(html).toContain('失败原因')
    expect(html).not.toContain('重投')

    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    expect(at(DELIVERIES_BODY_ID).style.display).toBe('none')
    expect(at(DELIVERIES_TOGGLE_ID).textContent).toBe(DELIVERIES_TOGGLE_CLOSED)
  })

  it('⭐ 取台账失败（403）⇒ **如实显示**后端文案，⛔ 不渲染成"还没有推送记录"', async () => {
    await mount()
    ready('GET /api/open/wecom-qr-app/deliveries/', () => {
      throw new BizError(403, '没有权限执行该操作')
    })
    at(DELIVERIES_TOGGLE_ID).click()
    await flush()
    expect(at(DELIVERIES_WARN_ID).textContent).toBe('没有权限执行该操作')
    expect(at(DELIVERIES_BODY_ID).innerHTML).toBe('')
    expect(at(DELIVERIES_BODY_ID).textContent).not.toContain('还没有推送记录')
  })

  // ── 停用 / 启用 / 轮换（都带二次确认）────────────────────────

  it('停用：二次确认里说清「第三方当场调不通」，PATCH **只发** state', async () => {
    await mount()
    at<HTMLButtonElement>(TOGGLE_BUTTON_ID).click()
    expect(LAYUI.confirms).toHaveLength(1)
    expect(LAYUI.confirms[0]).toContain('第三方当场调不通')
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('patch', '/api/open/wecom-qr-app/').data).toEqual({ state: 'disabled' })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe('已停用')
  })

  it('启用：反向（同一颗按钮，文案逐行不同）', async () => {
    appOnServer = { ...APP, state: 'disabled', state_text: '停用' }
    await mount()
    expect(at<HTMLButtonElement>(TOGGLE_BUTTON_ID).textContent).toBe('启用')
    at<HTMLButtonElement>(TOGGLE_BUTTON_ID).click()
    expect(LAYUI.confirms[0]).toContain('启用后第三方就能调它的接口了')
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('patch', '/api/open/wecom-qr-app/').data).toEqual({ state: 'enabled' })
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe('已启用')
  })

  it('轮换：确认里**保留**「旧密钥立刻作废」，且**没有**「只显示一次」那半句', async () => {
    await mount()
    at<HTMLButtonElement>(ROTATE_BUTTON_ID).click()
    expect(LAYUI.confirms[0]).toContain('旧密钥立刻作废')
    expect(LAYUI.confirms[0]).not.toContain('只显示')
    // 未点确定之前**一个写请求都不该发**（在途闸门只包住那次请求，⛔ 不包住确认框）。
    expect(mockedRequest.mock.calls.map((c) => (c[0] as Cfg).method)).toEqual(['get'])
  })

  // ── ⭐ 页内自检那道门（深链；设计文档 D10）─────────────────────

  it('⭐ 非账号管理员深链打开 ⇒ 提示无权 + **不注入标记** + 一个请求都不发（fail-closed）', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 2,
      username: 'synthetic-kefu',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      // 就算**持有**那个节点也不行：路由那第二道门是账号管理员（D10）。
      menu_permissions: ['system.pinduoduo_settings'],
      permissions: [],
    })
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    expect(root.innerHTML).toBe('')
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    // 也**没有**去取那把应用（被拒的页面不该先发一批请求）。
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('账号管理员**没**那个节点也照常能进（`canNode` 对账号管理员短路为真，与路由判据同源）', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 3,
      username: 'synthetic-account-admin',
      display_name: '合成账号管理员',
      identity_plane: 'tenant',
      is_tenant_admin: true,
      is_account_admin: true,
      roles: [],
      menu_permissions: [],
      permissions: [],
    })
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    // ⚠️ 这一条**不是**"被拒"——`canNode` 对账号管理员**短路为真**
    //    （`@/layui-island/perms.ts`，与壳层 `session.canNode` 同口径）⇒ 管理员照样能进，
    //    与路由那两道门（`permissions` 由 `guards.ts` 走同一个 `session.canNode`）**同源**。
    //    ⇒ 本用例钉的是「页内判据不是第二套」，⛔ 不是"非管理员也放行"（那一支由上面那条用例钉）。
    expect(root.innerHTML).not.toBe('')
    expect(LAYUI.messages).not.toContain(NO_ACCESS_MESSAGE)
  })

  it('页内自检用的节点码与 `config/routePermissions.ts` 那条**同值**（字面值钉，E-0024）', async () => {
    // ⚠️ 引的是 api 模块导出的**字面值比较**，不是"两边都引同一个常量"那种自指断言。
    const mod = (await import('@/api/wecomGroupQr')) as Record<string, unknown>
    expect(mod.WECOM_QR_APP_NODE).toBe('system.pinduoduo_settings')
    // 对照（防"模块没加载"造成的恒真）：同模块的另两个导出确实在。
    expect(Object.keys(mod)).toContain('getWecomQrApp')
    expect(Object.keys(mod)).toContain('listWecomQrDeliveries')
    // 运行时形状：五条端点的 URL 都在 `/api/open/wecom-qr-app/` 下，⛔ 没有 `permissions` / `games`
    // 那种「应用管理」页已废除的路径。
    const urls = [
      mod.WECOM_QR_APP_URL as string,
      (mod.wecomQrAppRotateSecretUrl as () => string)(),
      (mod.wecomQrAppDeliveriesUrl as () => string)(),
    ]
    for (const url of urls) {
      expect(url.startsWith('/api/open/wecom-qr-app/')).toBe(true)
      expect(url).not.toContain('permissions')
      expect(url).not.toContain('games')
    }
  })

  // ── ⛔ 页面上没有的东西（判据打在**标记**上）──────────────────

  it('⛔ 标记上没有删除入口、没有权限/游戏范围、没有名称输入框、没有"只显示一次"那类措辞', async () => {
    await mount()
    const html = buildHtml()
    // ① 「只能停用不能删」：连一个删除动作 / 删除按钮都没有。
    expect(html).not.toContain('lay-event="del"')
    expect(html).not.toContain('删除应用')
    // ② 权限设置 / 游戏范围是「应用管理」页 2026-10-08 已删掉的东西，⛔ 本页也不许有。
    expect(html).not.toContain('权限设置')
    expect(html).not.toContain('游戏范围')
    expect(html).not.toContain('权限树')
    // ③ 名称输入框：`name` 不在本端点 PATCH 的可改字段里 ⇒ 放一个改了不生效的框 = 静默不生效。
    expect(html).not.toContain('wecomQrName')
    // ④ 密钥**随时可取**（本页就显示明文）⇒ ⛔ 不许出现"只显示这一次 / 再也看不到"这类
    //    与实现相反的话。
    expect(html).not.toContain('只显示这一次')
    expect(html).not.toContain('只显示一次')
    expect(html).not.toContain('再也看不到')
    // ⑤ 能力说明与取票示例**在标记里**（不是某个点开才看得见的弹层里）。
    expect(html).toContain(CAPABILITY_NOTICE)
    //    ⚠️ 示例进的是 `<pre>`、走 `escapeHtml` ⇒ 标记里是**转义后**的形状（`<订单号>` → `&lt;订单号&gt;`）；
    //    原文形状打在**渲染后的纯文本**上（下面那一条整页 textContent 的断言）。
    expect(html).toContain('/api/open/v1/orders/&lt;订单号&gt;/images/ticket/')
    expect(root.textContent).toContain(TICKET_EXAMPLE)
    // ⑥ 整页可见文字里同样没有那两块（判据打在**渲染后的 DOM 纯文本**上：注释不进 textContent，
    //    这正是 E-0161 要求的口径——⛔ 不许拿"源码文件文本"当判据）。
    const text = root.textContent ?? ''
    expect(text).not.toContain('权限设置')
    expect(text).not.toContain('游戏范围')
    // 对照：本页**确实**渲染了内容（否则上面几条会因为"页面是空的"而恒真）。
    // ⚠️ 拿**六个区块的标题**当对照（⛔ 不拿应用名：§8.2 没要求把 `name` 上屏——
    //    用户看到它的地方是创建成功那句回报）。
    expect(text).toContain('接入信息')
    expect(text).toContain('推送台账')
    expect(text).toContain('能力与调用示例')
  })

  it('能力说明与取票示例写清了那两件事（只能读订单 / 取票只接受群聊二维码 + img_type=1）', async () => {
    await mount()
    const text = at(CAPABILITY_ID).textContent ?? ''
    expect(text).toContain('读订单')
    // ⚠️ 口径 = **取票那一跳**（业务方裁定「不补代码、改文案」；spec §7.2 改准块 / §9-15）：
    //    认领端点不重判图种 ⇒ ⛔ 这里不许写成「凭证级不变量」那类更强的承诺。
    expect(text).toContain('取票只接受群聊二维码')
    expect(text).toContain('img_type=1')
    // 取票示例：路径与 body 逐字对着实现（§7.2 的取票端点 + Bearer 令牌）。
    expect(TICKET_EXAMPLE).toContain('/api/open/v1/orders/<订单号>/images/ticket/')
    expect(TICKET_EXAMPLE).toContain('Authorization: Bearer')
    expect(TICKET_EXAMPLE).toContain('{"img_type": 1}')
  })

  // ── 纯函数（提示 / 回报 / 台账标记 / 时间格）──────────────────

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

  it('`callbackSaveReport` / `ipSaveReport`：各自**说破后果**（空 / 停用 ⇒ 一条都不会推）', () => {
    expect(callbackSaveReport('', 'enabled')).toContain('不会推送')
    expect(callbackSaveReport('   ', 'enabled')).toContain('不会推送')
    expect(callbackSaveReport('https://cb.example.com/tt', 'disabled')).toContain('不会推送')
    expect(callbackSaveReport('https://cb.example.com/tt', 'enabled')).toContain('已保存')
    expect(callbackSaveReport('https://cb.example.com/tt', 'enabled')).not.toContain('不会推送')
    expect(ipSaveReport('')).toContain('不限制来源 IP')
    expect(ipSaveReport('203.0.113.9')).toContain('只有白名单里的 IP')
    expect(ipSaveReport('  ')).toContain('不限制来源 IP')
  })

  it('`stateActionLabel` / `timeText`：判据打**机器值**、空值说人话（⛔ 不是空白格）', () => {
    expect(stateActionLabel(APP)).toBe('停用')
    expect(stateActionLabel({ ...APP, state: 'disabled', state_text: '停用' })).toBe('启用')
    // ⚠️ 判据**不看** `state_text`：中文名被改坏也不该影响那颗按钮的文案。
    expect(stateActionLabel({ ...APP, state_text: '随便什么中文' })).toBe('停用')
    expect(timeText(null)).toBe('—')
    expect(timeText('')).toBe('—')
    expect(timeText('2026-10-09T17:20:00+08:00')).toBe('2026-10-09 17:20:00')
  })

  it('⭐ `deliveriesHtml`：空列表说"还没有"；台账里的字符串一律转义；⛔ 没有重投按钮', () => {
    expect(deliveriesHtml([], 0)).toContain('还没有推送记录')
    // ① 来自后端的字符串一律转义（`reason` 是**对方响应里的原文**，最脏的就是它）。
    const evil: WecomQrDeliveryRow = {
      ...DELIVERY_FAILED,
      reason: '"><img src=x onerror=alert(1)>',
      event_text: '<script>alert(1)</script>',
    }
    const html = deliveriesHtml([evil], 1)
    expect(html).not.toContain('<img')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;img')
    expect(html).toContain('&lt;script&gt;')
    // ② 三种状态各自的中文名来自后端（`status_text`），三种都渲染得出来。
    const all = deliveriesHtml([DELIVERY_FAILED, DELIVERY_DONE, DELIVERY_PENDING], 3)
    expect(all).toContain('投递失败')
    expect(all).toContain('已投递')
    expect(all).toContain('待投递')
    // ③ ⛔ 本页**没有重投**（§7.3 五条端点里没有它）⇒ 标记里没有那颗按钮、也没有那一列。
    expect(all).not.toContain('重投')
    expect(all).not.toContain('data-retry-delivery')
    // ④ 只取第一页时如实说还差多少（⛔ 不假装这就是全部）。
    expect(deliveriesHtml([DELIVERY_FAILED], 42)).toContain('共 42 条，这里只显示最近 1 条')
  })

  it('`copyTextToClipboard`：没有剪贴板 ⇒ 明确回 `false`（⛔ 不静默）', () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const seen: boolean[] = []
    copyTextToClipboard('x', (ok) => seen.push(ok))
    expect(seen).toEqual([false])
  })
})
