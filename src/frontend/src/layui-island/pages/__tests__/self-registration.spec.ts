// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Page } from '@/api/unwrap'

/**
 * 【自助注册】island 页的契约测试（2026-10-16，spec §7-18；
 * `docs/specs/2026-10-16-self-registration-links-design.md` §4）。
 *
 * 这一页是**自有功能**（对标物的人员管理组没有这一页），没有证据件可对拍；本文件钉的是
 * **有真实后果**的约定（spec §7-18 原话的落点，一条不少）：
 *
 * 1. **三卡结构判据**：配置卡（游戏 tag checkbox + 开关 + 保存）、生成卡（按钮 + 完整链接位）、
 *    记录卡（7 列表头 + 行）；三条 island 陷阱按**运行期 DOM** 判（不是读源码）；
 * 2. **保存配置流**：勾游戏 + 开关 ⇒ `PUT config` 的载荷正确；一个都没勾 ⇒ 就地提示且不发；
 *    预填（GET 回来的勾选与开关）落到原生 `checked` 属性上；
 * 3. **生成复制流**：`POST invites` → `clipboard.writeText` 收到
 *    `location.origin + '/m/register?tk=' + token` → 成功/失败**双文案**；
 *    复制失败时完整链接**仍显示**（手动复制的退路，⛔ 不静默丢唯一一份明文）；
 * 4. **记录表渲染**：8 列、状态色标、未使用行的「—」格、空态、XSS 转义；
 *    「作废」只在未使用行画按钮、两跳 confirm（D-0253）。
 *
 * ## 手法（照 `feedback.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `layer.msg` 的
 *   **入参**才能被断言；`form.render` 也换成 spy（真渲染要真 layui.css）；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/selfRegistration` 与 `@/api/config`
 *   （URL、方法与载荷键的**唯一**拼装处）跑的是**真实现** ⇒ 「打到哪条 URL、body 里是什么键」
 *   这两条判据才有意义（把 api 模块也换成替身的话，那只是在断言自己传了一个对象）；
 * - 剪贴板：`navigator.clipboard.writeText` 换成可控替身（成功 / 拒绝两条路都驱动得到）。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G）在本文件里的落点
 *
 * ① 类名与选择器分成两个常量：本文件不写字面选择器，一律引页面导出的
 *    `PANEL_SELECTOR` / `FORM_SELECTOR` / `ROW_CLASS` 等（与 `buildHtml` 用的是同一份常量）；
 * ② 以 `(` 开头的语句一律前置分号；
 * ③ 先 `await bootstrapIsland(...)` 挂样式、**再**塞标记，`initIslandElements(layui)` 留在
 *    塞标记**之后** —— 挂载组用例按**运行期 DOM** 判（不是读源码）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：游戏名 / 人名 / 令牌都是编的，令牌用"43 字符级"的假形状，
 * 不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 探针元素用**保存按钮**的 id：它在 `buildHtml()` 的标记里，且必须在页面模块求值**之前**
  //    定下字面值（本工厂是那条 import 链的一环，引页面常量会读到尚未初始化的绑定）。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    seenByBootstrap,
    seenByInit,
    form: { render: vi.fn() },
    layer: {
      msg: vi.fn((text: string) => messages.push(text)),
      // ⚠️ `confirm` 默认**同步确认**（立刻回调 yes）——两跳连问在默认替身下都通过；
      //    「取消」那一侧的用例自己 `mockImplementation` 换掉（不调 cb = 用户点了取消）。
      confirm: vi.fn((_text: string, _opts: unknown, cb: (i: number) => void) => cb(101)),
      close: vi.fn(),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#selfRegSaveBtn') !== null)
    return { form: LAYUI.form, layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#selfRegSaveBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/**
 * 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`。
 * ⚠️ **必须连 `CSRF_HEADER_NAME` 一起给**：`@/api/http` 的其它导出有消费方
 * （`api/selfRegistration` 只用 `request`，但替身少导出会在模块加载期抛
 * 「No "CSRF_HEADER_NAME" export is defined」——与 `feedback.spec.ts` 同一条现场教训）。
 */
vi.mock('@/api/http', () => ({ request: vi.fn(), CSRF_HEADER_NAME: 'X-CSRFToken' }))
/** 身份：本页第一句就 `fetchMe()`（深链自检那道门）⇒ 必须可控。 */
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import {
  SELF_REG_CONFIG_URL,
  SELF_REG_INVITES_URL,
  selfRegInviteRevokeUrl,
  type SelfRegInviteRow,
} from '@/api/selfRegistration'
import { ROUTE_NODE_CODES } from '@/config/routePermissions'
import page, {
  FORBID_SWITCH_ID,
  FORM_SELECTOR,
  GAMES_BOX_ID,
  GENERATE_BUTTON_ID,
  LINK_BOX_ID,
  LIST_BODY_ID,
  LIST_HEAD_ID,
  NUMBER_PREFIX_INPUT_ID,
  NUMBER_START_INPUT_ID,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  PAGE_KEY,
  PANEL_SELECTOR,
  REVOKE_BUTTON_SELECTOR,
  REGISTER_PAGE_PATH,
  REFRESH_BUTTON_ID,
  ROW_CLASS,
  SAVE_BUTTON_ID,
  SELF_REG_NODE,
  STATUS_STYLE,
  __testables,
} from '../self-registration'

const { buildHtml, formatTime, gamesMarkup, listHtml } = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; data?: unknown }

/** 身份夹具：持 `system_user.add_edit` 节点的**人事**（非管理员——管理员会短路放行，钉不住节点门）。 */
const RENSHI = {
  id: 7,
  username: 'renshi-a',
  display_name: '人事甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [] as string[],
  permissions: [] as string[],
  menu_permissions: [SELF_REG_NODE],
}

/** 一款游戏（键名照 `api/config.ts::ConfigRow`；值全是**合成值**）。 */
function game(id: number, name: string): { id: number; name: string; sort_order: number; is_active: boolean; created_at: string } {
  return { id, name, sort_order: id, is_active: true, created_at: '2026-10-01T00:00:00+08:00' }
}

const GAMES: Page<ReturnType<typeof game>> = {
  items: [game(1, '测试游戏甲'), game(2, '测试游戏乙'), game(3, '测试游戏丙')],
  total: 3,
  page: 1,
  page_size: 200,
}

/** 一条记录（键名照 spec §3.2 的逐字契约；值全是**合成值**）。 */
function inviteRow(over: Partial<SelfRegInviteRow> = {}): SelfRegInviteRow {
  return {
    id: 11,
    created_at: '2026-10-16T10:00:00+08:00',
    created_by_name: '人事甲',
    status: 'unused',
    expires_at: '2026-10-23T10:00:00+08:00',
    consumed_at: null,
    consumed_by_name: null,
    chosen_game_names: [],
    ...over,
  }
}

/** 默认首屏：空配置（没存过）+ 全量游戏 + 空记录。 */
const DEFAULT_ROUTES: Record<string, unknown> = {
  [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
  'GET /api/business/games/': GAMES,
  [`GET ${SELF_REG_INVITES_URL}`]: [],
}

function flush(): Promise<void> {
  // 挂载是多跳的（fetchMe → bootstrap → 塞标记 → config/games 并行 → invites）：连放几轮宏任务。
  return (async () => {
    for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  })()
}

/** 按 `METHOD URL` 造一次响应路由；**没有备好的响应就抛**（⛔ 不是静默回 undefined）。 */
function respond(routes: Record<string, unknown>): void {
  mockedRequest.mockImplementation((config: Cfg) => {
    const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
    if (!(key in routes)) throw new Error(`用例没有备 ${key} 的响应`)
    const value = routes[key]
    // ⚠️ 失败那一支给的是**函数**：`Promise.reject(...)` 写在字面量里会在装配期就建出
    //    未处理的 rejection（可能把别的用例带红）——照 feedback.spec 同一写法。
    if (typeof value === 'function') return (value as () => Promise<never>)()
    return Promise.resolve(value)
  })
}

/** 全部请求（`METHOD URL`）。 */
function requests(): string[] {
  return mockedRequest.mock.calls.map((c) => {
    const cfg = c[0] as Cfg
    return `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url}`
  })
}

let root: HTMLElement
let writeText: ReturnType<typeof vi.fn>
const ORIGINAL_EXEC_COMMAND = document.execCommand

/** 剪贴板替身（默认成功；用例自己改成拒绝来驱动"复制失败"那一支）。 */
function installClipboard(): void {
  writeText = vi.fn(async () => undefined)
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText },
  })
}

function click(el: HTMLElement): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
}

/** 勾 / 取消一个游戏（layui 渲染被替身掉了 ⇒ 直接驱动原生 input 的 property）。 */
function setGameChecked(rootEl: HTMLElement, id: number, checked: boolean): void {
  const input = rootEl.querySelector<HTMLInputElement>(
    `#${GAMES_BOX_ID} input[name="games"][value="${id}"]`,
  )
  if (!input) throw new Error(`页面上没有游戏 ${id} 的勾选框`)
  input.checked = checked
}

function setSwitchChecked(rootEl: HTMLElement, checked: boolean): void {
  const input = rootEl.querySelector<HTMLInputElement>(`#${FORBID_SWITCH_ID}`)
  if (!input) throw new Error('页面上没有默认开关')
  input.checked = checked
}

beforeEach(() => {
  vi.clearAllMocks()
  // ⚠️ `clearAllMocks` 只清**调用记录**、不还原实现：「取消」侧用例给 `confirm` 换过的
  //    mockImplementation 会残留到下一条用例（实测踩过：失败用例的 confirm 再也不回调）。
  //    每条用例开工前先把「同步确认」的默认行为装回去。
  vi.mocked(LAYUI.layer.confirm).mockImplementation(
    (_text: string, _opts: unknown, cb: (i: number) => void) => cb(101),
  )
  LAYUI.messages.length = 0
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  document.body.innerHTML = ''
  root = document.createElement('div')
  document.body.appendChild(root)

  mockedFetchMe.mockResolvedValue(RENSHI)
  respond(DEFAULT_ROUTES)
  installClipboard()
  document.execCommand = vi.fn(() => true) as unknown as typeof document.execCommand
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.execCommand = ORIGINAL_EXEC_COMMAND
  document.body.innerHTML = ''
})

/** 挂页面（`await` 完首屏那次列表请求）。 */
async function mount(): Promise<void> {
  await page.mount(root, { onNavigate: () => {} })
  await flush()
}

describe('挂载：三条 island 陷阱与首屏', () => {
  it('先挂 bootstrapIsland、再塞标记、最后 initIslandElements（按运行期 DOM 判，不是读源码）', async () => {
    await mount()
    // ③a：`bootstrapIsland` 被调用时标记**还不在**（先挂样式，防 FOUC）。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    // ③b：`initIslandElements` 被调用时标记**已经在**（它要按 `lay-*` 初始化）。
    expect(LAYUI.seenByInit).toEqual([true])
    // 三卡的容器都在（不是「什么都没渲染也算过」）。
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(root.querySelector(FORM_SELECTOR)).not.toBeNull()
    expect(root.querySelector(`#${GAMES_BOX_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${LINK_BOX_ID}`)).not.toBeNull()
    expect(root.querySelector(`#${LIST_BODY_ID}`)).not.toBeNull()
  })

  it('① 类名与选择器是两个常量：`class="…"` 里放的是**类名**、选择器另取（`class=".x"` 会永远匹配不到）', () => {
    const host = document.createElement('div')
    host.innerHTML = buildHtml()
    expect(host.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(host.innerHTML).not.toContain('class=".')
  })

  it('本页声明的节点 code 与 `config/routePermissions.ts` 的登记**同值**（⛔ 不靠注释自证）', () => {
    // iframe 里读不到 Vue 路由的 `meta`，页面自检用的是本文件那个字面量；
    // 两处漂移的症状是「路由放你进来、页面自己把你挡出去」，且没有任何网会报。
    expect(SELF_REG_NODE).toBe('system_user.add_edit')
    expect(ROUTE_NODE_CODES[PAGE_KEY]).toEqual([SELF_REG_NODE])
  })

  it('没有节点时**不注入标记**（深链自检；与路由那道门同源，⛔ 不是第二套判据）', async () => {
    mockedFetchMe.mockResolvedValue({ ...RENSHI, menu_permissions: [] })
    await page.mount(root, { onNavigate: () => {} })
    await flush()
    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    // ⛔ 注入必须排在门禁之后：先注入就会让被拒的深链看到整个页面壳。
    expect(root.querySelector(PANEL_SELECTOR)).toBeNull()
  })

  it('预填：GET 配置回来勾选与开关落到原生 `checked` 属性上（form.render 读的是它）', async () => {
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [1], forbid_orders_default: true, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
    })
    await mount()
    const game1 = root.querySelector<HTMLInputElement>(
      `#${GAMES_BOX_ID} input[name="games"][value="1"]`,
    )
    const game2 = root.querySelector<HTMLInputElement>(
      `#${GAMES_BOX_ID} input[name="games"][value="2"]`,
    )
    expect(game1?.hasAttribute('checked')).toBe(true)
    expect(game2?.hasAttribute('checked')).toBe(false)
    expect(root.querySelector<HTMLInputElement>(`#${FORBID_SWITCH_ID}`)?.hasAttribute('checked')).toBe(true)
    // 游戏标签的 `title` 是**游戏名**（照 user-adduser 的形状：value=id、title=名字）。
    expect(game1?.getAttribute('title')).toBe('测试游戏甲')
  })
})

describe('保存配置流', () => {
  it('勾两款游戏保存 ⇒ PUT 载荷 `{game_ids, forbid_orders_default:false}` 正确，成功有提示', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`PUT ${SELF_REG_CONFIG_URL}`]: { game_ids: [1, 3], forbid_orders_default: false, number_prefix: '', number_start: null },
    })
    setGameChecked(root, 1, true)
    setGameChecked(root, 3, true)
    const before = mockedRequest.mock.calls.length
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    const put = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .find((c) => c.method === 'PUT')
    expect(put, '应当发过一条 PUT').toBeDefined()
    expect(put!.url).toBe(SELF_REG_CONFIG_URL)
    // ⚠️ 载荷钉**值**（E-0024）：勾选集合 + 开关布尔都来自 DOM，不是夹具回声。
    expect(put!.data).toEqual({ game_ids: [1, 3], forbid_orders_default: false, number_prefix: '', number_start: null })
    expect(LAYUI.messages).toContain(MESSAGES.saved)
    expect(mockedRequest.mock.calls.length).toBe(before + 1)
  })

  it('开关打开再保存 ⇒ `forbid_orders_default: true`（开关 False 也是合法值，两条都要能发）', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`PUT ${SELF_REG_CONFIG_URL}`]: { game_ids: [2], forbid_orders_default: true, number_prefix: '', number_start: null },
    })
    setGameChecked(root, 2, true)
    setSwitchChecked(root, true)
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    const put = mockedRequest.mock.calls.map((c) => c[0] as Cfg).find((c) => c.method === 'PUT')
    expect(put!.data).toEqual({ game_ids: [2], forbid_orders_default: true, number_prefix: '', number_start: null })
  })

  it('编号两栏（spec §2.1）：GET 回什么填什么；保存时随 PUT 原样发出（前缀+起始号）', async () => {
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [1], forbid_orders_default: false, number_prefix: 'sq', number_start: 20001 },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`PUT ${SELF_REG_CONFIG_URL}`]: { game_ids: [1], forbid_orders_default: false, number_prefix: 'sq', number_start: 20002 },
    })
    await mount()
    // 预填：两栏都按 GET 的值落（⛔ 不是空白也不是编的默认值）。
    expect((root.querySelector<HTMLInputElement>(`#${NUMBER_PREFIX_INPUT_ID}`))!.value).toBe('sq')
    expect((root.querySelector<HTMLInputElement>(`#${NUMBER_START_INPUT_ID}`))!.value).toBe('20001')
    setGameChecked(root, 1, true)
    // 改起始编号再保存：载荷里是 DOM 里的新值（不是 GET 的回声）。
    ;(root.querySelector<HTMLInputElement>(`#${NUMBER_START_INPUT_ID}`))!.value = '20002'
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    const put = mockedRequest.mock.calls.map((c) => c[0] as Cfg).find((c) => c.method === 'PUT')
    expect(put, '应当发过一条 PUT').toBeDefined()
    expect(put!.data).toEqual({
      game_ids: [1],
      forbid_orders_default: false,
      number_prefix: 'sq',
      number_start: 20002,
    })
  })

  it('编号两栏留空保存 ⇒ `number_prefix: ""`、`number_start: null` 照发（没配也发，两列都空 = 未配置）', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`PUT ${SELF_REG_CONFIG_URL}`]: { game_ids: [1], forbid_orders_default: false, number_prefix: '', number_start: null },
    })
    setGameChecked(root, 1, true)
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    const put = mockedRequest.mock.calls.map((c) => c[0] as Cfg).find((c) => c.method === 'PUT')
    expect(put!.data).toEqual({
      game_ids: [1],
      forbid_orders_default: false,
      number_prefix: '',
      number_start: null,
    })
  })

  it('编号前缀填了数字/中文 ⇒ 就地提示带「编号前缀」字段名且**不发 PUT**（体验层预检；服务端 §2.1 同判据）', async () => {
    await mount()
    setGameChecked(root, 1, true)
    for (const bad of ['sq1', '编号前缀']) {
      LAYUI.messages.length = 0
      const before = mockedRequest.mock.calls.length
      ;(root.querySelector<HTMLInputElement>(`#${NUMBER_PREFIX_INPUT_ID}`))!.value = bad
      click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
      await flush()
      expect(LAYUI.messages).toContain('编号前缀只能是 1~5 个英文字母')
      expect(requests().slice(before)).toEqual([])
    }
  })

  it('起始编号填了 0 ⇒ 就地提示带「起始编号」字段名且**不发 PUT**（体验层预检）', async () => {
    await mount()
    setGameChecked(root, 1, true)
    ;(root.querySelector<HTMLInputElement>(`#${NUMBER_START_INPUT_ID}`))!.value = '0'
    const before = mockedRequest.mock.calls.length
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain('起始编号必须是 ≥ 1 的整数')
    expect(requests().slice(before)).toEqual([])
  })

  it('一个游戏都没勾 ⇒ 就地提示（文案带「游戏」字段名）且**不发 PUT**（体验层预检）', async () => {
    await mount()
    setSwitchChecked(root, true)
    const before = mockedRequest.mock.calls.length
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.noGames)
    expect(requests().slice(before)).toEqual([])
  })

  it('保存失败 ⇒ 如实提示后端文案（⛔ 不是静默什么都不发生）', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`PUT ${SELF_REG_CONFIG_URL}`]: () => Promise.reject(new Error('请先勾选至少一款注册游戏')),
    })
    setGameChecked(root, 1, true)
    click(root.querySelector<HTMLElement>(`#${SAVE_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain('请先勾选至少一款注册游戏')
  })
})

describe('生成复制流', () => {
  /** spec §3.2：响应里的 token（43 字符级的假形状，合成值）。 */
  const TOKEN = 'aZ3_fake-token_9x8y7z6_5432109876543210abcdef'
  const CREATED = { token: TOKEN, expires_at: '2026-10-23T10:00:00+08:00' }
  const EXPECTED_URL = `${location.origin}${REGISTER_PAGE_PATH}${TOKEN}`

  it('生成 ⇒ POST 无请求体、剪贴板收到 `origin + /m/register?tk=<token>`、成功 toast、页面显示完整链接', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`POST ${SELF_REG_INVITES_URL}`]: CREATED,
    })
    click(root.querySelector<HTMLElement>(`#${GENERATE_BUTTON_ID}`)!)
    await flush()

    const post = mockedRequest.mock.calls.map((c) => c[0] as Cfg).find((c) => c.method === 'POST')
    expect(post, '应当发过一条 POST invites').toBeDefined()
    expect(post!.url).toBe(SELF_REG_INVITES_URL)
    // spec §3.2 第 1 步原话「无请求体」⇒ data 是 undefined（⛔ 不是发一个空对象当作"没载荷"）。
    expect(post!.data).toBeUndefined()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(EXPECTED_URL)
    expect(LAYUI.messages).toContain(MESSAGES.linkCopied)
    // 完整链接**显示在页面上**（复制失败时的手动退路，spec §4 卡 2 原话「显示完整链接」）。
    const linkBox = root.querySelector<HTMLElement>(`#${LINK_BOX_ID}`)
    expect(linkBox?.textContent).toContain(EXPECTED_URL)
    expect(linkBox?.textContent).toContain('2026-10-23 10:00:00')
  })

  it('自动复制失败（writeText 拒绝 + execCommand 也失败）⇒ 换文案、完整链接**仍显示**（手动退路）', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`POST ${SELF_REG_INVITES_URL}`]: CREATED,
    })
    writeText.mockRejectedValue(new Error('denied'))
    document.execCommand = vi.fn(() => false) as unknown as typeof document.execCommand
    click(root.querySelector<HTMLElement>(`#${GENERATE_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.copyFailed)
    expect(LAYUI.messages).not.toContain(MESSAGES.linkCopied)
    const linkBox = root.querySelector<HTMLElement>(`#${LINK_BOX_ID}`)
    expect(linkBox?.textContent).toContain(EXPECTED_URL)
  })

  it('POST 失败 ⇒ 如实提示后端文案、不碰剪贴板、链接位**不**出现半截链接', async () => {
    await mount()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [],
      [`POST ${SELF_REG_INVITES_URL}`]: () =>
        Promise.reject(new Error('请先在上方配置至少一款游戏再生成链接')),
    })
    click(root.querySelector<HTMLElement>(`#${GENERATE_BUTTON_ID}`)!)
    await flush()
    expect(LAYUI.messages).toContain('请先在上方配置至少一款游戏再生成链接')
    expect(writeText).not.toHaveBeenCalled()
    expect(root.querySelector<HTMLElement>(`#${LINK_BOX_ID}`)?.textContent).not.toContain(REGISTER_PAGE_PATH)
  })
})

describe('链接与注册记录表', () => {
  it('渲染 8 列表头与行：已使用行显示注册人/注册时间/勾选游戏，状态是色标，操作列只在未使用行有按钮', async () => {
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [
        inviteRow({
          id: 11,
          status: 'used',
          consumed_at: '2026-10-17T09:30:00+08:00',
          consumed_by_name: '打手小张',
          chosen_game_names: ['测试游戏甲', '测试游戏丙'],
        }),
        inviteRow({ id: 12, status: 'expired' }),
      ],
    })
    await mount()

    const head = root.querySelector<HTMLElement>(`#${LIST_HEAD_ID}`)
    expect(Array.from(head!.querySelectorAll('th')).map((th) => th.textContent)).toEqual([
      '生成时间',
      '生成人（开户人）',
      '状态',
      '有效期至',
      '注册进来的人',
      '注册时间',
      '勾选游戏',
      '操作',
    ])
    const rows = Array.from(root.querySelectorAll<HTMLElement>(`.${ROW_CLASS}`))
    expect(rows).toHaveLength(2)
    const [used, expired] = rows
    // 已使用行：状态色标 + 注册进来的人 + 注册时间 + 勾选游戏（顿号连接）。
    expect(used!.querySelector('span.layui-badge')?.className).toBe(STATUS_STYLE.used.badge)
    expect(used!.querySelector('span.layui-badge')?.textContent).toBe('已使用')
    expect(used!.textContent).toContain('打手小张')
    expect(used!.textContent).toContain('2026-10-17 09:30:00')
    expect(used!.textContent).toContain('测试游戏甲、测试游戏丙')
    // 已过期行：状态是"已过期"、没人注册 ⇒ 四格都是「—」（3 个空值格 + 操作列）。
    expect(expired!.querySelector('span.layui-badge')?.textContent).toBe('已过期')
    expect(expired!.querySelectorAll('td span[style]')).toHaveLength(4)
    expect(expired!.textContent).not.toContain('打手小张')
    // ⭐ D-0253：已消费 / 已过期的行**不画**「作废」按钮（没有可作废的东西）。
    expect(used!.querySelector(REVOKE_BUTTON_SELECTOR)).toBeNull()
    expect(expired!.querySelector(REVOKE_BUTTON_SELECTOR)).toBeNull()
  })

  it('未使用行：三格「—」（spec §4 原话）+ 操作列画「作废」按钮，状态是"未使用"', async () => {
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [inviteRow({ id: 13, status: 'unused' })],
    })
    await mount()
    const row = root.querySelector<HTMLElement>(`.${ROW_CLASS}`)!
    expect(row.querySelector('span.layui-badge')?.className).toBe(STATUS_STYLE.unused.badge)
    expect(row.querySelector('span.layui-badge')?.textContent).toBe('未使用')
    expect(row.textContent).toContain('人事甲')
    expect(row.querySelectorAll('td span[style]')).toHaveLength(3)
    const revokeBtn = row.querySelector<HTMLButtonElement>(REVOKE_BUTTON_SELECTOR)
    expect(revokeBtn, '未使用行应当画「作废」按钮').not.toBeNull()
    expect(revokeBtn!.textContent).toBe('作废')
    // 按钮指认到它自己的行（点击委托靠 `data-selfreg-row` 找 id）。
    expect(revokeBtn!.closest(`.${ROW_CLASS}`)?.getAttribute('data-selfreg-row')).toBe('13')
  })

  it('空列表 ⇒ 空态文案说出来（空态 ≠ 取数失败），且**表头仍在**（一片白会被读成"页面坏了"）', async () => {
    await mount()
    const body = root.querySelector<HTMLElement>(`#${LIST_BODY_ID}`)!
    expect(body.textContent).toContain(MESSAGES.empty)
    expect(root.querySelectorAll(`.${ROW_CLASS}`).length).toBe(0)
    expect(root.querySelectorAll(`#${LIST_HEAD_ID} th`).length).toBe(8)
  })

  it('点「刷新」⇒ 真的又取了一次记录（不是摆设按钮）', async () => {
    await mount()
    const before = requests().filter((r) => r === `GET ${SELF_REG_INVITES_URL}`).length
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [inviteRow({ id: 14 })],
    })
    click(root.querySelector<HTMLElement>(`#${REFRESH_BUTTON_ID}`)!)
    await flush()
    expect(requests().filter((r) => r === `GET ${SELF_REG_INVITES_URL}`).length).toBe(before + 1)
    expect(root.querySelectorAll(`.${ROW_CLASS}`).length).toBe(1)
  })

  it('取记录失败 ⇒ 如实提示（⛔ 不是静默空列表）', async () => {
    mockedRequest.mockRejectedValue(new Error('网络异常，请检查连接'))
    await mount()
    expect(LAYUI.messages).toContain('网络异常，请检查连接')
  })

  it('状态色标的色值与 vendored `layui.css` **对拍**（⛔ 不许凭记忆写色值）', () => {
    // 判据打在**依赖自己的 CSS** 上：`STATUS_STYLE` 里那个 badge 类必须真的存在于 layui.css，
    // 且色点色值必须是**同一条规则里的真实声明值**——green / orange 的底色即呈现色
    // （`background-color`）；gray 那一档的底是浅灰 `#fafafa`、色点用它的**文字色** `#5f5f5f`
    // （同一条规则里的 `color`）⇒ 两者的色点与色标读起来是同一种颜色，漂了就红。
    const css = readFileSync(resolve(process.cwd(), 'public/layui/css/layui.css'), 'utf8')
    for (const [state, style] of Object.entries(STATUS_STYLE)) {
      const cls = style.badge.split(' ').find((c) => c.startsWith('layui-bg-'))!
      const rule = new RegExp(`\\.${cls}\\{([^}]*)\\}`).exec(css)
      expect(rule, `${cls} 在 layui.css 里找不到`).not.toBeNull()
      // ⚠️ `noUncheckedIndexedAccess` 下 `rule[1]` 是 `string | undefined`：已在上一行断言非 null，
      //    这里给 `?? ''` 收窄（真缺组时 bg/fg 双双 undefined ⇒ 下面那条 expect 会红）。
      const decls = rule![1] ?? ''
      const bg = /background-color:(#[0-9a-f]{6})/.exec(decls)?.[1]
      const fg = /;color:(#[0-9a-f]{6})/.exec(decls)?.[1]
      const hit = [bg, fg].find((c) => c === style.color)
      expect(
        hit,
        `${state} 的色点色值 ${style.color} 不在 ${cls} 的声明里（bg=${String(bg)} fg=${String(fg)}）`,
      ).toBeDefined()
    }
  })
})

describe('作废链接流（D-0253）', () => {
  const REVOKE_URL = selfRegInviteRevokeUrl(11)

  /** 首屏就有一条未使用行（id=11，inviteRow 的默认形状）挂好按钮。 */
  async function mountWithUnusedRow(): Promise<void> {
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [inviteRow({ id: 11, status: 'unused' })],
    })
    await mount()
  }

  it('点「作废」：两跳 confirm 逐字问两遍、都确认 ⇒ POST revoke、成功提示、列表刷新', async () => {
    await mountWithUnusedRow()
    // 成功响应 + 刷新那一次 GET（页面在 POST 成功后会重取列表）。
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [inviteRow({ id: 11, status: 'expired' })],
      [`POST ${REVOKE_URL}`]: null,
    })
    const beforeLists = requests().filter((r) => r === `GET ${SELF_REG_INVITES_URL}`).length
    LAYUI.messages.length = 0
    click(root.querySelector<HTMLElement>(REVOKE_BUTTON_SELECTOR)!)
    await flush()

    // 两跳 confirm，问句逐字（⛔ 文案不是随便一句话——两连问是删用户同款的防误触形状）。
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(2)
    expect(LAYUI.layer.confirm.mock.calls[0]?.[0]).toBe(MESSAGES.revokeConfirmFirst)
    expect(LAYUI.layer.confirm.mock.calls[1]?.[0]).toBe(MESSAGES.revokeConfirmSecond)
    // 请求打到行自己的 revoke 端点、POST 无请求体。
    const posts = mockedRequest.mock.calls
      .map((c) => c[0] as Cfg)
      .filter((c) => c.method === 'POST' && c.url === REVOKE_URL)
    expect(posts, '应当发过一条 POST revoke').toHaveLength(1)
    expect(posts[0]!.data).toBeUndefined()
    expect(LAYUI.messages).toContain(MESSAGES.revoked)
    // 成功后刷新列表（状态落地为「已过期」、按钮消失都靠这次重取）。
    expect(requests().filter((r) => r === `GET ${SELF_REG_INVITES_URL}`).length).toBeGreaterThan(beforeLists)
  })

  it('第一跳就取消（confirm 不确认）⇒ 什么都不发', async () => {
    await mountWithUnusedRow()
    vi.mocked(LAYUI.layer.confirm).mockImplementation((_t: string, _o: unknown, _cb: (i: number) => void) => 100)
    click(root.querySelector<HTMLElement>(REVOKE_BUTTON_SELECTOR)!)
    await flush()
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(requests().filter((r) => r === `POST ${REVOKE_URL}`)).toHaveLength(0)
  })

  it('第二跳取消（只有第一跳确认）⇒ 什么都不发（两连问缺一不可）', async () => {
    await mountWithUnusedRow()
    let hops = 0
    vi.mocked(LAYUI.layer.confirm).mockImplementation((_t: string, _o: unknown, cb: (i: number) => void) => {
      hops += 1
      if (hops === 1) cb(101) // 第一跳确认；第二跳的 cb 永不被调 = 用户点了取消
      return 100 + hops
    })
    click(root.querySelector<HTMLElement>(REVOKE_BUTTON_SELECTOR)!)
    await flush()
    expect(hops).toBe(2)
    expect(requests().filter((r) => r === `POST ${REVOKE_URL}`)).toHaveLength(0)
  })

  it('作废失败 ⇒ 如实提示后端文案（已消费 / 已过期各有明确的一句，D-0253）', async () => {
    await mountWithUnusedRow()
    respond({
      [`GET ${SELF_REG_CONFIG_URL}`]: { game_ids: [], forbid_orders_default: false, number_prefix: '', number_start: null },
      'GET /api/business/games/': GAMES,
      [`GET ${SELF_REG_INVITES_URL}`]: [inviteRow({ id: 11, status: 'unused' })],
      [`POST ${REVOKE_URL}`]: () => Promise.reject(new Error('该链接已被使用，无法作废')),
    })
    LAYUI.messages.length = 0
    click(root.querySelector<HTMLElement>(REVOKE_BUTTON_SELECTOR)!)
    await flush()
    expect(LAYUI.messages).toContain('该链接已被使用，无法作废')
  })
})

describe('纯函数：标记与判据（可被单测证伪的那些）', () => {
  it('时间格式化：`T` 换成空格、截到秒（时区偏移不动）', () => {
    expect(formatTime('2026-10-16T10:00:00+08:00')).toBe('2026-10-16 10:00:00')
  })

  it('来自服务端的文本**一律转义**（游戏名 / 人名 / 游戏清单都进 `innerHTML`）', () => {
    // 游戏名进的是 `title` **属性** ⇒ 判据打在 `gamesMarkup` 的**返回串**上（标记在进
    // innerHTML **之前**就已转义）；⚠️ 不能打在解析后的 `innerHTML` 上——HTML 属性值
    // 序列化不转义 `<`，`&lt;` 会被解析回原文再原样吐出来（判据会恒假）。
    const markup = gamesMarkup([game(1, '<img src=x onerror=alert(1)>')], [])
    expect(markup).toContain('title="&lt;img src=x onerror=alert(1)&gt;"')
    // 解析后：不产生元素注入，且**原文**作为属性值原样保留（没有被丢掉）。
    const host = document.createElement('div')
    host.innerHTML = markup
    expect(host.querySelector('img')).toBeNull()
    expect(host.querySelector('input')!.getAttribute('title')).toBe('<img src=x onerror=alert(1)>')

    const listHost = document.createElement('div')
    listHost.innerHTML = listHtml([
      {
        id: 11,
        created_at: '2026-10-16T10:00:00+08:00',
        created_by_name: '<b>人事</b>',
        status: 'used',
        expires_at: '2026-10-23T10:00:00+08:00',
        consumed_at: '2026-10-17T09:30:00+08:00',
        consumed_by_name: '<script>bad()</script>',
        chosen_game_names: ['<i>游戏</i>'],
      },
    ])
    expect(listHost.querySelector('script')).toBeNull()
    expect(listHost.querySelector('b')).toBeNull()
    expect(listHost.querySelector('i')).toBeNull()
    // 转义之后的**原文**仍然看得见（不是被丢掉）。
    expect(listHost.textContent).toContain('<b>人事</b>')
    expect(listHost.textContent).toContain('<script>bad()</script>')
  })

  it('空记录的 `listHtml` 走空态一格（colspan = 8），非空走行', () => {
    expect(listHtml([])).toContain('还没有生成过注册链接')
    expect(listHtml([])).toContain('colspan="8"')
    expect(listHtml([inviteRow()])).toContain(`class="${ROW_CLASS}"`)
  })

  it('生成链接的路径常量是 `/m/register?tk=`（与 m 端路由同一条路径；漂了链接就是 404）', () => {
    // ⛔ 不从 m 端模块 import（跨端 import 会把 Vue SFC 链拖进 island 页）——
    //    这里钉**字面值**，与 `src/m/router.ts` 里那条 `path: '/register'` 人工对齐；
    //    m 端那侧由 `m/pages/__tests__/SelfRegisterPage.spec.ts` 钉 query 语义。
    expect(REGISTER_PAGE_PATH).toBe('/m/register?tk=')
  })
})
