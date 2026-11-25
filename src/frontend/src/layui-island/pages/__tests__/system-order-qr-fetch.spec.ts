// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OrderQrConfigRow } from '@/api/orderQrFetch'

/**
 * 【第三方平台 → 马上来查码对接】island 页的契约测试（2026-10-31，D-0348）。
 *
 * 这一页是**自有功能**（对标物里没有【第三方平台】这一组）；本文件钉的是**有真实后果**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    载入 → 保存（**api_key 空串也发** =「留空不改」的请求形状钉值）一整条链；
 * 2. ⭐ **401 闩横幅**：闩住 ⇒ 横幅出现且文案逐字；未闩 ⇒ 页面上没有横幅节点；
 * 3. **四条 island 陷阱**（§5-G）：类名/选择器两个常量、`(` 前置分号（保存那条分支）、
 *    挂样式 → 塞标记 → init（**运行期**判据：两处各自记录当时页面上有没有标记）、
 *    本页无定时器（各用例自收口）；
 * 4. ⭐ **页内自检那道门**（照微信公众号设置页）：非账号管理员（哪怕持节点）深链打开 ⇒
 *    msg 一句无权 + **不注入标记** + **一个请求都不发**（fail-closed）；
 * 5. ⛔ **掩码不回写**：载入后 api_key 输入框是**空**（把掩码填回去 = 下次保存把掩码当新值），
 *    掩码只显示在旁边的说明行。
 *
 * ## 手法（照 `__tests__/system-wechatmp-settings.spec.ts`，⛔ 不自己发明一套）
 *
 * - `vi.mock('@/layui/island')`：jsdom 加载不了真 layui，换可控替身；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`api/orderQrFetch.ts` 跑**真实现**，
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义；
 * - `vi.mock('@/api/auth')`：页面第一句就 `fetchMe()`（自检门）⇒ 必须可控。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：基址 / 密钥 / 时间戳全是编的（域名是 RFC 2606 保留域）。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const formRenders: Array<{ type?: string; filter?: string }> = []
  // ⚠️ 挂载顺序的**运行期**判据（`AGENTS.md` §5-G 陷阱 ③）。
  //    选择器写字面值：本工厂在页面模块求值**之前**跑，引常量会读到未初始化绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    formRenders,
    seenByBootstrap,
    seenByInit,
    layer: {},
    form: {
      render: vi.fn((type?: string, filter?: string) => {
        formRenders.push({ type, filter })
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#orderQrFetchSaveBtn') !== null)
    return { layer: LAYUI.layer, form: LAYUI.form }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#orderQrFetchSaveBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

/** 身份：页面第一句就 `fetchMe()`（自检那道门）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  API_KEY_INPUT_ID,
  BASE_URL_INPUT_ID,
  CAPABILITY_LINES,
  CARD_SELECTOR,
  ENABLED_SWITCH_ID,
  LATCH_BANNER_SELECTOR,
  MASKED_TEXT_ID,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  SAVE_BUTTON_ID,
  SAVE_HINT_TEXT,
  TITLE,
  buildHtml,
  latchBannerHtml,
} from '../system-order-qr-fetch'

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; data?: unknown }

/** 响应路由：按 `METHOD URL` 认领；**没有备好响应就抛**（⛔ 不是静默回 undefined）。 */
let routes: Record<string, (data?: unknown) => unknown>

function ready(key: string, fn: (data?: unknown) => unknown): void {
  routes[key] = fn
}

// ── 夹具（**全部合成**）────────────────────────────────────────────────────

/** 已配置 + 启用 + 密钥已存（只有掩码）+ 401 闩住。 */
const CONFIG_LATCHED: OrderQrConfigRow = {
  configured: true,
  enabled: true,
  base_url: 'https://peer.example.test',
  api_key_masked: 'syn****123',
  auth_failed_at: '2026-10-31T10:00:00+08:00',
  updated_at: '2026-10-31T10:00:00+08:00',
}

/** 未配置（新租户常态）+ 没闩。 */
const CONFIG_EMPTY: OrderQrConfigRow = {
  configured: false,
  enabled: false,
  base_url: '',
  api_key_masked: '',
  auth_failed_at: null,
  updated_at: null,
}

function flush(): Promise<void> {
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
    .filter(
      (c) => (c.method ?? 'get').toLowerCase() === method.toLowerCase() && c.url === url,
    )
  if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
  return found[found.length - 1]!
}

function at<T extends HTMLElement>(id: string): T {
  const el = document.querySelector<T>(`#${id}`)
  if (el === null) throw new Error(`页面上找不到 #${id}`)
  return el
}

describe('马上来查码对接 island 页（system-order-qr-fetch）', () => {
  let root: HTMLElement
  /** 服务端**此刻**的配置行（保存会改它）。 */
  let configOnServer: OrderQrConfigRow

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
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

    routes = {
      // 默认路由：GET 配置回已闩的行（横幅分支）；保存回服务端**新状态**。
      'GET /api/business/order-qr/config/': () => configOnServer,
    }
    mockedRequest.mockImplementation((cfg: Cfg) => {
      const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url}`
      const handler = routes[key]
      if (handler === undefined) {
        return Promise.reject(new Error(`测试没有备好 ${key} 的响应`))
      }
      return Promise.resolve(handler(cfg.data))
    })
  })

  afterEach(async () => {
    await flush()
  })

  async function mount(): Promise<void> {
    // 本页每个用例挂在一次性 root 上、用例结束整体清空 ⇒ 不需要 cleanup。
    await page.mount(root, {} as never)
  }

  it('载入：灌值 + 掩码不回写输入框 + 401 闩横幅（文案逐字）+ form.render 被调', async () => {
    configOnServer = CONFIG_LATCHED
    await mount()
    // 标记进来了（两张卡）。
    expect(document.querySelectorAll(CARD_SELECTOR).length).toBeGreaterThanOrEqual(2)
    expect(at<HTMLInputElement>(BASE_URL_INPUT_ID).value).toBe(CONFIG_LATCHED.base_url)
    // ⛔ 掩码**不回写**输入框（防把掩码当新值存进去）。
    expect(at<HTMLInputElement>(API_KEY_INPUT_ID).value).toBe('')
    expect(at(MASKED_TEXT_ID).textContent).toContain(CONFIG_LATCHED.api_key_masked)
    expect((at<HTMLInputElement>(ENABLED_SWITCH_ID) as HTMLInputElement).checked).toBe(true)
    // 401 闩横幅：出现且文案逐字。
    expect(document.querySelector(LATCH_BANNER_SELECTOR)).not.toBeNull()
    expect(document.querySelector(LATCH_BANNER_SELECTOR)!.textContent).toContain(
      MESSAGES.latchBanner,
    )
    // 开关重渲被调（layui switch）。
    expect(
      LAYUI.formRenders.some((r) => r.type === 'checkbox' && r.filter === 'orderQrFetchForm'),
    ).toBe(true)
    // 挂载顺序（陷阱 ③ 的运行期判据）：bootstrap 时页面上**还没有**标记、init 时**已有**。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('未配置：没有横幅、掩码行为空（新租户常态，不是错误）', async () => {
    configOnServer = CONFIG_EMPTY
    await mount()
    expect(document.querySelector(LATCH_BANNER_SELECTOR)).toBeNull()
    expect(at(MASKED_TEXT_ID).textContent).toBe('')
    expect((at<HTMLInputElement>(ENABLED_SWITCH_ID) as HTMLInputElement).checked).toBe(false)
  })

  it('保存：PATCH 到配置端点，api_key 空串也发（留空 = 不改的请求形状）；成功后重灌新状态', async () => {
    configOnServer = CONFIG_LATCHED
    await mount()
    /** 服务端保存后的新状态（运营者换了密钥 ⇒ 闩已清）。 */
    const saved: OrderQrConfigRow = { ...CONFIG_LATCHED, auth_failed_at: null }
    ready('PATCH /api/business/order-qr/config/', () => {
      configOnServer = saved
      return saved
    })

    at<HTMLInputElement>(BASE_URL_INPUT_ID).value = 'https://peer2.example.test'
    at(SAVE_BUTTON_ID).click()
    await flush()

    const sent = callTo('PATCH', '/api/business/order-qr/config/')
    expect(sent.data).toEqual({
      enabled: true,
      base_url: 'https://peer2.example.test',
      api_key: '',
    })
    // 成功提示 + 闩横幅被摘掉（新状态未闩）。
    expect(LAYUI.messages).toContain(MESSAGES.saved)
    expect(document.querySelector(LATCH_BANNER_SELECTOR)).toBeNull()
  })

  it('保存失败：错误原样上屏（不假装成功）', async () => {
    configOnServer = CONFIG_EMPTY
    await mount()
    ready('PATCH /api/business/order-qr/config/', () =>
      Promise.reject(new Error('对端基址必须是 http:// 或 https:// 开头的完整地址')),
    )
    at(SAVE_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages).toContain('对端基址必须是 http:// 或 https:// 开头的完整地址')
    expect(LAYUI.messages).not.toContain(MESSAGES.saved)
  })

  it('页内自检：非账号管理员（哪怕持节点）⇒ 无权提示 + 不注入标记 + 一个请求都不发', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 2,
      username: 'synthetic-clerk',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      menu_permissions: ['system.pinduoduo_settings'],
      permissions: [],
    })
    await mount()
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    expect(document.querySelectorAll(CARD_SELECTOR).length).toBe(0)
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('纯函数：buildHtml 的钉子（提示逐字 / 能力说明三行 / 横幅标记只在 latched 时有）', () => {
    const html = buildHtml()
    expect(html).toContain(SAVE_HINT_TEXT)
    for (const line of CAPABILITY_LINES) expect(html).toContain(line)
    expect(html).not.toContain('orderQrFetchLatchBanner') // 整页标记不含横幅（由 applyRow 增删）
    const banner = latchBannerHtml(MESSAGES.latchBanner)
    expect(banner).toContain(MESSAGES.latchBanner)
    expect(banner).toContain('orderQrFetchLatchBanner')
    expect(TITLE).toBe('马上来查码对接')
  })
})
