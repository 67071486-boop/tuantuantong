// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { WechatMpConfigRow } from '@/api/wechatmp'

/**
 * 【第三方平台 → 微信公众号设置】island 页的契约测试（2026-10-19，D-0268 自开发集成线 T6）。
 *
 * 这一页是**自有功能**（对标物里没有【第三方平台】这一组）；本文件钉的是**有真实后果**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    载入 → 保存（**留空 = 保持不变**，S4 的请求形状钉值）→ 复制服务器配置 →
 *    同步预设菜单 → 删除菜单（二次确认）一整条链；
 * 2. ⭐ **认证提示横幅**（业务方点名）：文案**逐字**、常驻（钉在标记上）；
 * 3. **四条 island 陷阱**（§5-G）：类名/选择器两个常量、`(` 前置分号（同步删除那条分支）、
 *    挂样式 → 塞标记 → init（运行期判据）、本页无定时器（各用例自收口）；
 * 4. ⭐ **页内自检那道门**（照企微群码 D10）：非账号管理员（哪怕持节点）深链打开 ⇒
 *    msg 一句无权 + **不注入标记** + **一个请求都不发**（fail-closed）；
 * 5. **S6**：服务器 IP 未配置（空串）⇒ 显示「向部署方查询」占位，配置了 ⇒ 显示原文。
 *
 * ## 手法（照 `__tests__/wecom-group-qr.spec.ts`，⛔ 不自己发明一套）
 *
 * - `vi.mock('@/layui/island')`：jsdom 加载不了真 layui，换可控替身；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`api/wechatmp.ts` 跑**真实现**，
 *   「打到哪条 URL、body 里是什么键」这两条判据才有意义；
 * - `vi.mock('@/api/auth')`：页面第一句就 `fetchMe()`（自检门）⇒ 必须可控。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：appid / token / aes_key / 模板 id 全是编的。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const confirms: string[] = []
  const confirmOks: Array<() => void> = []
  const formRenders: Array<{ type?: string; filter?: string }> = []
  // ⚠️ 挂载顺序的**运行期**判据（`AGENTS.md` §5-G 陷阱 ③）。
  //    选择器写字面值：本工厂在页面模块求值**之前**跑，引常量会读到未初始化绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    confirms,
    confirmOks,
    formRenders,
    seenByBootstrap,
    seenByInit,
    layer: {
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
    LAYUI.seenByBootstrap.push(document.querySelector('#wechatMpSaveBtn') !== null)
    return { layer: LAYUI.layer, form: LAYUI.form }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#wechatMpSaveBtn') !== null)
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
  APPSECRET_INPUT_ID,
  BANNER_SELECTOR,
  MENU_BODY_ID,
  MENU_DELETE_BUTTON_ID,
  MENU_SYNC_BUTTON_ID,
  MESSAGES,
  MODE_SELECT_ID,
  NO_ACCESS_MESSAGE,
  NOT_READY_HINT_ID,
  SERVER_IP_PLACEHOLDER,
  SERVER_IP_TEXT_ID,
  SERVER_TOKEN_TEXT_ID,
  SAVE_BUTTON_ID,
  VERIFY_BANNER,
  __testables,
} from '../system-wechatmp-settings'

const { buildHtml, copyTextToClipboard, menuBodyHtml, notReadyMessage } = __testables

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

/** 已就绪的配置：启用 + 三件凭据齐 + 模板 id + 服务器 IP 已配。 */
const CONFIG_READY: WechatMpConfigRow = {
  appid: 'wx0000000000000000',
  appsecret_masked: 'abcd********wxyz',
  token: 'SYNTHETIC-token-0123456789',
  aes_key: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ',
  mode: 'safe',
  enabled: true,
  template_id: 'SYNTHETIC-template-0001',
  ready: true,
  server: {
    callback_url: 'https://tenant.example.com/api/wechatmp/callback',
    token: 'SYNTHETIC-token-0123456789',
    aes_key: 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ',
    server_ip: '203.0.113.10',
  },
}

/** 未就绪（新租户默认行）+ 服务器 IP 未配置（S6 的占位分支）。 */
const CONFIG_NOT_READY: WechatMpConfigRow = {
  appid: '',
  appsecret_masked: '',
  token: '',
  aes_key: '',
  mode: 'plain',
  enabled: false,
  template_id: '',
  ready: false,
  server: { callback_url: '', token: '', aes_key: '', server_ip: '' },
}

function flush(): Promise<void> {
  // 取数链是多跳的（点击 → request → 再 request → 上屏）：连放几轮宏任务。
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

function at<T extends HTMLElement>(id: string): T {
  const el = document.querySelector<T>(`#${id}`)
  if (el === null) throw new Error(`页面上找不到 #${id}`)
  return el
}

describe('微信公众号设置 island 页（system-wechatmp-settings）', () => {
  let root: HTMLElement
  /** 服务端**此刻**的配置行（保存 / 刷新会改它）。 */
  let configOnServer: WechatMpConfigRow

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.messages.length = 0
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
    configOnServer = CONFIG_READY
    ready('GET /api/wechatmp/config/', () => configOnServer)
    // PUT 把入参**并回**那一行（空串 = 保持不变 的语义由页面保证：空串就不覆盖）。
    ready('PUT /api/wechatmp/config/', (data) => {
      const p = (data ?? {}) as Record<string, unknown>
      configOnServer = {
        ...configOnServer,
        appid: String(p.appid ?? configOnServer.appid),
        // ⚠️ 空串 = 保持（S4）：测试替身按后端语义实现。appsecret 本身不回显（⛔ 类型里没有这个键），
        //    替身只把它折进 appsecret_masked 的语义里。
        token: p.token === '' ? configOnServer.token : String(p.token),
        aes_key: p.aes_key === '' ? configOnServer.aes_key : String(p.aes_key),
        mode: (p.mode as WechatMpConfigRow['mode']) ?? configOnServer.mode,
        enabled: Boolean(p.enabled),
        template_id: String(p.template_id ?? ''),
      }
      configOnServer.appsecret_masked = 'abcd********wxyz'
      configOnServer.ready = true
      return { ready: true }
    })
    ready('POST /api/wechatmp/menu/sync/', () => ({
      menu: { button: [{ name: '绑定账号' }, { name: '我的订单' }] },
      wechat: { errcode: 0, errmsg: 'ok' },
    }))
    ready('GET /api/wechatmp/menu/', () => ({
      wechat: { menu: { button: [{ name: '绑定账号' }, { name: '我的订单' }] }, is_menu_open: 1 },
    }))
    ready('DELETE /api/wechatmp/menu/', () => ({ wechat: { errcode: 0, errmsg: 'ok' } }))
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
    document.body.innerHTML = ''
  })

  async function mount(): Promise<void> {
    const cleanup = await page.mount(root, { onNavigate: vi.fn() })
    // cleanup 只在卸载时用；测试结束靠 document.body.innerHTML = '' 收口。
    void cleanup
    await flush()
  }

  // ── 横幅与标记 ────────────────────────────────────────────────────────

  it('认证提示横幅常驻且文案逐字（业务方点名）', async () => {
    await mount()
    const banner = document.querySelector(BANNER_SELECTOR)
    expect(banner).not.toBeNull()
    expect(banner!.textContent).toContain(VERIFY_BANNER)
  })

  it('挂载顺序（陷阱 ③）：先挂样式（标记还没塞）→ 再塞标记 → 最后 init', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('未就绪时显示 fail-closed 提示；就绪时那句为空', async () => {
    configOnServer = CONFIG_NOT_READY
    await mount()
    expect(at(NOT_READY_HINT_ID).textContent).toContain('未就绪')
    expect(notReadyMessage(true)).toBe('')
    expect(notReadyMessage(false)).not.toBe('')
  })

  // ── S4：保存的请求形状 ────────────────────────────────────────────────

  it('保存：三个凭据留空 ⇒ 发出去的就是空串（留空 = 保持不变）；其余字段照发', async () => {
    await mount()
    // 装配后凭据输入框**刻意留空**（掩码不回填）。
    expect(at<HTMLInputElement>(APPSECRET_INPUT_ID).value).toBe('')
    at(SAVE_BUTTON_ID).click()
    await flush()
    const put = callTo('put', '/api/wechatmp/config/')
    expect(put.data).toEqual({
      appid: 'wx0000000000000000',
      appsecret: '',
      token: '',
      aes_key: '',
      mode: 'safe',
      enabled: true,
      template_id: 'SYNTHETIC-template-0001',
    })
    // 保存成功后重取配置并回报（⛔ 不是"操作成功"四个字）。
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.saved)
  })

  it('保存失败（业务码 1000 的中文）如实显示，不美化', async () => {
    await mount()
    routes['PUT /api/wechatmp/config/'] = () => {
      throw new Error('安全 / 兼容模式必须先填写 EncodingAESKey（43 位）')
    }
    at(SAVE_BUTTON_ID).click()
    await flush()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe('安全 / 兼容模式必须先填写 EncodingAESKey（43 位）')
  })

  it('模式下拉的三档逐字来自后端 EncryptionMode', async () => {
    await mount()
    expect(at<HTMLSelectElement>(MODE_SELECT_ID).value).toBe('safe')
    const values = [...at<HTMLSelectElement>(MODE_SELECT_ID).options].map((o) => o.value)
    expect(values).toEqual(['plain', 'compatible', 'safe'])
  })

  // ── 服务器配置信息卡（S6）────────────────────────────────────────────

  it('服务器 IP 已配置 ⇒ 显示原文；未配置 ⇒ 显示「向部署方查询」占位', async () => {
    await mount()
    expect(at(SERVER_IP_TEXT_ID).textContent).toBe('203.0.113.10')
    configOnServer = CONFIG_NOT_READY
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    await mount()
    expect(at(SERVER_IP_TEXT_ID).textContent).toBe(SERVER_IP_PLACEHOLDER)
  })

  it('服务器 Token 原文上屏（要复制进公众平台；AppSecret 刻意不回原文）', async () => {
    await mount()
    expect(at(SERVER_TOKEN_TEXT_ID).textContent).toBe(CONFIG_READY.server.token)
    expect(document.body.innerHTML).not.toContain('appsecret') // ⛔ 页面标记里没有 AppSecret 出口
  })

  // ── 菜单卡 ────────────────────────────────────────────────────────────

  it('同步预设菜单 → 重取菜单并回报；删除走二次确认后发 DELETE', async () => {
    await mount()
    at(MENU_SYNC_BUTTON_ID).click()
    await flush()
    expect(callTo('post', '/api/wechatmp/menu/sync/')).toBeTruthy()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.menuSynced)
    expect(at(MENU_BODY_ID).innerHTML).toContain('绑定账号')
    expect(at(MENU_BODY_ID).innerHTML).toContain('我的订单')

    at(MENU_DELETE_BUTTON_ID).click()
    // ⚠️ 删除有二次确认：没点确认之前**不发** DELETE。
    expect(LAYUI.confirms.length).toBe(1)
    expect(mockedRequest.mock.calls.filter((c) => (c[0] as Cfg).method === 'delete').length).toBe(0)
    LAYUI.confirmOks[0]!()
    await flush()
    expect(callTo('delete', '/api/wechatmp/menu/')).toBeTruthy()
    expect(LAYUI.messages[LAYUI.messages.length - 1]).toBe(MESSAGES.menuDeleted)
  })

  it('菜单拉取失败 ⇒ 菜单卡如实显示失败文案（⛔ 不渲染成"还没有菜单"）', async () => {
    routes['GET /api/wechatmp/menu/'] = () => {
      throw new Error('读取菜单失败：微信接口返回 errcode=46003')
    }
    await mount()
    expect(at(MENU_BODY_ID).innerHTML).toContain('errcode=46003')
  })

  // ── 页内自检门（D10 同款，fail-closed）────────────────────────────────

  it('非账号管理员（哪怕持节点）深链打开 ⇒ msg 无权 + 不注入标记 + 零请求', async () => {
    mockedFetchMe.mockResolvedValue({
      id: 2,
      username: 'synthetic-staff',
      display_name: '合成客服',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      menu_permissions: ['system.pinduoduo_settings'],
      permissions: [],
    })
    await mount()
    expect(LAYUI.messages).toEqual([NO_ACCESS_MESSAGE])
    expect(document.querySelector('#wechatMpSaveBtn')).toBeNull() // ⛔ 标记没注入
    expect(mockedRequest).not.toHaveBeenCalled() // ⛔ 一个请求都不发
  })

  // ── 纯函数 ────────────────────────────────────────────────────────────

  it('menuBodyHtml：错误分支 / 空菜单分支 / 正常分支三态', () => {
    expect(menuBodyHtml(null, '读取菜单失败')).toContain('读取菜单失败')
    expect(menuBodyHtml({}, '')).toContain('还没有菜单')
    const ok = menuBodyHtml({ menu: { button: [{ name: '绑定账号' }] } }, '')
    expect(ok).toContain('绑定账号')
    expect(ok).toContain('原始回包')
  })

  it('buildHtml 的标记里没有「AppSecret 明文出口」与「试发按钮」', () => {
    const html = buildHtml()
    expect(html).toContain('留空 = 保持不变')
    expect(html).not.toContain('发送测试消息')
    expect(html).toContain(VERIFY_BANNER)
  })

  it('copyTextToClipboard：无 clipboard ⇒ 回调 ok=false（不静默）', () => {
    const done = vi.fn()
    copyTextToClipboard('x', done)
    expect(done).toHaveBeenCalledWith(false)
  })
})
