// @vitest-environment jsdom
/**
 * 大厅页「自动刷新频率」（本线新增，spec `docs/specs/2026-10-08-hall-throw-and-autorefresh-design.md`
 * §3.3 + §4.2 的判据 15–21）。
 *
 * ⚠️ **对标这一页没有自动刷新**（读码：`insiderecorderlist.js` 全文件零 `setInterval`）⇒ 这是
 * **新增功能、不是复刻**；所以本文件里没有「与对标对拍」的用例，只有**行为判据**与
 * 「证据件一个字没动」这条留档判据。
 *
 * 手法照同目录的 `order-insiderecorderlist.spec.ts`：`vi.mock` 掉 `@/layui/island`（jsdom 里
 * 加载不了真 layui），`table.render` / `table.reload` 都是 `vi.fn()` ⇒ 能抓 `mock.calls` 拿
 * options、并手动调 `opts.done(...)` 来模拟「这一发回来了」。
 *
 * 用假定时器（`vi.useFakeTimers()`）驱动 interval；⛔ 不 sleep、不真等 1 秒。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  AUTO_REFRESH_BUTTON_CLASS,
  AUTO_REFRESH_BUTTON_HTML,
  AUTO_REFRESH_OFF,
  AUTO_REFRESH_PANEL_TITLE,
  AUTO_REFRESH_RADIO_FILTER,
  AUTO_REFRESH_STEPS_MS,
  AUTO_REFRESH_STORAGE_KEY,
  autoRefreshLabel,
  mountHallPage,
  normalizeAutoRefreshMs,
} from '../order-insiderecorderlist'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-insiderecorderlist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-insiderecorderlist.html'),
  'utf8',
)

/** layui 的**可控替身**（与 `order-insiderecorderlist.spec.ts` 同形，只留本文件用得到的）。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    msg,
    formHandlers,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: { render: vi.fn(), reload: vi.fn(), on: vi.fn() },
    layer: { msg, open: vi.fn(), close: vi.fn(), confirm: vi.fn(), prompt: vi.fn(), tips: vi.fn() },
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
    initIslandElements: vi.fn(),
  }
})

/** 字典 / 权限 / 取数一律打桩：本文件测的是**定时器与互斥**，零真网络（含 `fetch` 证据件那一路）。 */
vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return {
    ...actual,
    fetchGames: vi.fn(async () => [
      { id: 3, name: '原神', sort_order: 1, is_active: true, created_at: '' },
    ]),
    fetchServers: vi.fn(async () => [
      { id: 7, name: '天空岛', game: 3, sort_order: 1, is_active: true, created_at: '' },
    ]),
  }
})

vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return {
    ...actual,
    fetchMe: vi.fn(async () => ({
      id: 7,
      username: 'taker-a',
      display_name: '打手甲',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: ['代练员'],
      permissions: [],
      menu_permissions: ['grab_hall', 'grab_hall.take_order'],
      game_ids: [3],
    })),
  }
})

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/**
 * 本文件里**所有**已挂载页面的卸载函数（`afterEach` 统一收口）。
 *
 * ⚠️ 这不是洁癖：页面会在 `document` 上挂 `visibilitychange`（文档级、`root.innerHTML=''` 摘不掉），
 * 而 `LAYUI` 替身是**文件级单例**（`vi.mock` 的工厂只跑一次）⇒ 上一个用例没卸载干净的页面
 * 会在下一个用例的 `advanceTimersByTime` 里继续打**同一个** `table.reload` 替身，
 * 症状是「关着也在刷」「一发变三发」这种**看起来像被测代码坏了**的假红。
 */
const MOUNTED: (() => void)[] = []

/** 本文件里 `setInterval` 返回过的句柄（用例收尾时兜底清掉，防止假定时器被 `useRealTimers` 丢弃时留下疑问）。 */
/**
 * 定时器的**记账把手**：在 `vi.useFakeTimers()` **之后**包装 `window.setInterval` / `clearInterval`
 * （`vi.spyOn` 包的是假的实现 ⇒ 拿得到句柄）。用例靠它查「起了几个」与「卸载时清没清」。
 *
 * ⚠️ **实测踩过的坑（E-xxxx 族）**：`vi.useFakeTimers()` **之前**抓下来的 `setInterval` 再包一层
 * （`window.setInterval = wrapper`）会让页面起一个**真 Node `Timeout` 对象**——`advanceTimersByTime`
 * 推不动它，症状是「选完 1 秒之后一发都不发」，看起来像被测代码坏了。所以包装必须**排在
 * `useFakeTimers()` 之后**（那时 `window.setInterval` 已经是假实现）。
 */
const TIMERS = vi.hoisted(() => ({ created: [] as unknown[], cleared: [] as unknown[] }))

/** 挂载整页（证据件走全局 fetch 桩）。 */
async function mountHall(): Promise<{ root: HTMLElement; cleanup: () => void }> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
  MOUNTED.push(cleanup)
  return { root, cleanup }
}

/** 按钮（页内那颗 `#autoRefreshBtn`）。 */
function autoBtn(root: HTMLElement): HTMLElement {
  const el = root.querySelector<HTMLElement>('#autoRefreshBtn')
  expect(el, '页内找不到 #autoRefreshBtn').not.toBeNull()
  return el!
}

/** 造一棵与证据件同形的筛选树（本页真正读取的 4 个控件；占位 `value=""`，见既有 spec 的注释）。 */
function setFilters(
  root: HTMLElement,
  values: { game?: string; server?: string; keyword?: string; project?: string },
): void {
  const set = (sel: string, v: string | undefined): void => {
    if (v === undefined) return
    const el = root.querySelector<HTMLInputElement | HTMLSelectElement>(sel)
    expect(el, `页内找不到 ${sel}`).not.toBeNull()
    el!.value = v
  }
  set('select#gameName', values.game)
  set('select#gameArea', values.server)
  set('[name="key1"]', values.keyword)
  set('[name="project"]', values.project)
}

/**
 * 打开设置面板：点页内那颗按钮，返回 `layer.open` 的实参。
 *
 * ⚠️ 点的是**真按钮**（`root` 上挂的监听器），⛔ 不是直接调内部函数 —— 否则「按钮点了没反应」
 * 这类**静默**缺陷测不出来（`AGENTS.md` §5-G 的三条陷阱就是为它写的）。
 */
function openPanel(root: HTMLElement): Record<string, unknown> {
  const before = LAYUI.layer.open.mock.calls.length
  autoBtn(root).dispatchEvent(new MouseEvent('click', { bubbles: true }))
  expect(LAYUI.layer.open.mock.calls.length, '点了按钮但 layer.open 没被调用').toBe(before + 1)
  // ⚠️ 取**这一次**的实参（⛔ 不是 `calls[0]`：同一个用例里选两档时会拿到第一次那份，
  //    于是「面板反映当前档」这类判据会看错对象）。
  return LAYUI.layer.open.mock.calls[before]![0] as Record<string, unknown>
}

/**
 * 走**真交互**选一档：从面板标记里造出那颗 radio（jsdom 里没有真 layui 的皮肤渲染），
 * 派发 `change`（layui 的 `form.on('radio(x)')` 就是 change 委托），再调用页面**自己登记**的回调。
 *
 * ⚠️ 这样链路是「真 DOM 事件 → 页面登记的处理函数 → 状态变化」，与真人操作同形；
 * 只调内部函数会漏掉「选择器写错 / 回调没登记」这类静默缺陷。
 */
function chooseStep(root: HTMLElement, ms: number): void {
  const opts = openPanel(root)
  // 从**页面真实产出的**标记里取那一段（⛔ 不在用例里另拼一份 HTML：那就测不到实现写的是什么）。
  const host = document.createElement('div')
  host.innerHTML = String(opts.content)
  const input = host.querySelector<HTMLInputElement>(`input[value="${ms}"]`)
  expect(input, `面板里没有 ${ms} 这一档`).not.toBeNull()
  // 反射真实选中的那一档（layui 的皮肤会把它同步成 checked；jsdom 里手工补上，判据只关心回调收到的值）
  const handler = LAYUI.formHandlers.get(`radio(${AUTO_REFRESH_RADIO_FILTER})`)
  expect(handler, '页面没有登记 radio 回调（点了没反应）').toBeTypeOf('function')
  input!.checked = true
  handler!({ value: String(ms), elem: input })
}

/** 最近一次 `table.reload` 的整个 payload。 */
function lastReload(): { where: Record<string, unknown>; page: Record<string, unknown> } & Record<
  string,
  unknown
> {
  const calls = LAYUI.table.reload.mock.calls
  expect(calls.length, 'table.reload 没有被调用').toBeGreaterThan(0)
  const payload = calls[calls.length - 1]![1] as Record<string, unknown>
  return payload as never
}

/**
 * jsdom 里 `document.hidden` 是 **getter、不可直接赋值**（`Object.defineProperty` 才能改）。
 * 用完必须还原（`afterEach` 里做），否则会污染同文件后续用例。
 */
const originalHidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden')
function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
}

beforeEach(() => {
  // 假定时器：interval 由 `advanceTimersByTime` 驱动（⛔ 不真等 1 秒）。
  vi.useFakeTimers()
  // 记账把手（**必须在 `useFakeTimers()` 之后**，见 `TIMERS` 的注释）。页面写的是**裸**
  // `setInterval(...)`，在 jsdom 里就是 `window` 上这个 ⇒ 包装它能拦到。
  TIMERS.created.length = 0
  TIMERS.cleared.length = 0
  const fakeSet = window.setInterval.bind(window)
  const fakeClear = window.clearInterval.bind(window)
  vi.spyOn(window, 'setInterval').mockImplementation(((
    handler: TimerHandler,
    timeout?: number,
  ) => {
    const id = fakeSet(handler, timeout)
    TIMERS.created.push(id)
    return id
  }) as typeof window.setInterval)
  vi.spyOn(window, 'clearInterval').mockImplementation(((id?: number) => {
    TIMERS.cleared.push(id)
    fakeClear(id)
  }) as typeof window.clearInterval)
  // 每个用例从「没存过偏好」开始（真 `localStorage` 会跨用例残留）。
  window.localStorage.clear()
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.msg.mockClear()
  LAYUI.form.render.mockClear()
  LAYUI.layer.open.mockClear()
  LAYUI.formHandlers.clear()
  setHidden(false)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-insiderecorderlist.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  // 先把所有已挂载页面卸载（摘文档级监听 + 清定时器），再清兜底句柄，最后才丢假定时器环境。
  for (const cleanup of MOUNTED.splice(0)) cleanup()
  TIMERS.created.length = 0
  TIMERS.cleared.length = 0
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  // `hidden` 还原成 jsdom 原型上那份（不是 `delete`：删掉它会让后继用例连 `document.hidden` 都读不到）。
  if (originalHidden) Object.defineProperty(Document.prototype, 'hidden', originalHidden)
  document.body.innerHTML = ''
  window.localStorage.clear()
})

/* ── 纯函数（第一个失败点最容易定位的地方先钉） ── */

describe('档位与文案（纯函数）', () => {
  it('允许值就是 6 档 + 关闭；业务方要的 1 秒 / 2 秒都在（R2）', () => {
    expect(AUTO_REFRESH_STEPS_MS).toEqual([1000, 2000, 5000, 10000, 30000, 60000])
    expect(AUTO_REFRESH_OFF).toBe(0)
  })

  it('缓存值不在集合里 ⇒ 一律当关闭（fail-safe，⛔ 不抛、⛔ 不截断成邻近档）', () => {
    // 合法档位（`localStorage` 里恒是字符串 ⇒ 字符串形态是唯一的正常读入形状）
    for (const ms of AUTO_REFRESH_STEPS_MS) {
      expect(normalizeAutoRefreshMs(String(ms))).toBe(ms)
      expect(normalizeAutoRefreshMs(ms)).toBe(ms)
    }
    // 非法值：`500` 是**最容易出错**的那一个（它离 1000 最近 —— 截断成邻近档就是替用户改了设置）
    for (const bad of ['500', 500, 'abc', '-1000', -1000, '0', 1.5, '', '  ', null, undefined, {}, []]) {
      expect(normalizeAutoRefreshMs(bad), `${String(bad)} 应被当成关闭`).toBe(AUTO_REFRESH_OFF)
    }
    // ⛔ 不抛：这些输入全部原样返回 0，没有任何异常
    expect(normalizeAutoRefreshMs(Symbol('x') as unknown)).toBe(AUTO_REFRESH_OFF)
  })

  it('按钮文案始终反映状态：关闭 / 10秒', () => {
    expect(autoRefreshLabel(0)).toBe('自动刷新频率：关闭')
    expect(autoRefreshLabel(10000)).toBe('自动刷新频率：10秒')
    expect(autoRefreshLabel(1000)).toBe('自动刷新频率：1秒')
  })

  it('按钮标记 = spec §3.3 第 1 条的逐字形态（类名 / id / 文案）', () => {
    expect(AUTO_REFRESH_BUTTON_CLASS).toBe('layui-btn layui-btn-xs layui-btn-normal')
    expect(AUTO_REFRESH_BUTTON_HTML).toBe(
      '<a class="layui-btn layui-btn-xs layui-btn-normal" id="autoRefreshBtn">自动刷新频率：关闭</a>',
    )
  })
})

/* ── 判据 15 ── */

describe('15 按钮运行期注入：在 `.refresh` 之后；证据件一个字没动', () => {
  it('按钮存在，且紧跟在 `.refresh` 之后（DOM 位置判据）', async () => {
    const { root, cleanup } = await mountHall()
    const btn = autoBtn(root)
    const refresh = root.querySelector('.refresh')
    expect(refresh, '页内找不到证据件那颗 .refresh').not.toBeNull()
    // ① 紧随其后（同一层级的下一个兄弟）
    expect(refresh!.nextElementSibling).toBe(btn)
    // ② 位置判据（比「是兄弟」更强：顺序错了也红）
    expect(
      refresh!.compareDocumentPosition(btn) & Node.DOCUMENT_POSITION_FOLLOWING,
      '按钮不在 .refresh 之后',
    ).toBeTruthy()
    cleanup()
  })

  it('证据件那颗 `.refresh` 的 class / 文案**逐字未变**（改证据件 = 伪造留档）', async () => {
    const { root, cleanup } = await mountHall()
    const refresh = root.querySelector('.refresh')!
    expect(refresh.getAttribute('class')).toBe('layui-btn layui-btn-xs layui-btn-danger refresh')
    expect(refresh.getAttribute('title')).toBe('刷新当前数据')
    expect(refresh.textContent).toBe('刷新列表')
    cleanup()
  })

  it('证据件原文里**没有**这颗按钮（运行期注入，不是复刻；grep 级判据）', () => {
    expect(EVIDENCE).not.toContain('autoRefreshBtn')
    expect(EVIDENCE).not.toContain('自动刷新频率')
  })

  it('挂载后**只注入一颗**（没有重复注入 / 没有第二颗同 id）', async () => {
    const { root, cleanup } = await mountHall()
    expect(root.querySelectorAll('#autoRefreshBtn')).toHaveLength(1)
    cleanup()
  })

  it('按钮文案挂载时 = 关闭（没存过偏好）', async () => {
    const { root, cleanup } = await mountHall()
    expect(autoBtn(root).textContent).toBe('自动刷新频率：关闭')
    cleanup()
  })

  it('点它 → layer.open 的 type/title/area 逐项为 1 / 「自动刷新频率」/ 「320px × auto」，7 档单选', async () => {
    const { root, cleanup } = await mountHall()
    const opts = openPanel(root)
    expect(opts.type).toBe(1)
    expect(opts.title).toBe(AUTO_REFRESH_PANEL_TITLE)
    expect(opts.area).toEqual(['320px', 'auto'])
    const host = document.createElement('div')
    host.innerHTML = String(opts.content)
    const values = [...host.querySelectorAll('input')].map((i) => (i as HTMLInputElement).value)
    expect(values).toEqual(['0', '1000', '2000', '5000', '10000', '30000', '60000'])
    const texts = [...host.querySelectorAll('input')].map((i) => i.getAttribute('title'))
    expect(texts).toEqual(['关闭', '1 秒', '2 秒', '5 秒', '10 秒', '30 秒', '60 秒'])
    cleanup()
  })
})

/* ── 判据 16 ── */

describe('16 选 1 秒 ⇒ 1000ms 后 reload，且保持当前页与当前筛选', () => {
  it('page.curr = 当前页（第 3 页），where = 当前筛选', async () => {
    const { root, cleanup } = await mountHall()
    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    // 先把「当前页」推到第 3 页（真人翻页时 layui 会用 `done(res, curr, count, origin)` 交回真实页码）
    ;(cfg.done as (r: unknown, curr: number, count: number) => void)(undefined, 3, 40)
    // 当前筛选：游戏（挂载时默认第一条）+ 搜索词
    setFilters(root, { keyword: '角色名' })

    chooseStep(root, 1000)
    expect(autoBtn(root).textContent).toBe('自动刷新频率：1秒')
    // 还没到点：一发都不发
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    const payload = lastReload()
    expect(payload.where).toEqual({ game: 3, keyword: '角色名' })
    // 当前页保持第 3 页（不是跳回第 1 页）；`page` 里**没有** `limit`（layui 的「条/页」不被盖回）
    expect(payload.page).toEqual({ curr: 3 })
    cleanup()
  })

  it('再走一拍 ⇒ 又发（定时器真的在跑，不是只发一次）', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 2000)
    vi.advanceTimersByTime(2000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // 让这一发「回来」（要拿到那一发传进去的 `done`）
    const first = LAYUI.table.reload.mock.calls[0]![1] as { done?: (...a: unknown[]) => void }
    expect(first.done, '自动刷新那一发没有传 done（在途标记永远清不掉）').toBeTypeOf('function')
    first.done!(undefined, 1, 0)
    vi.advanceTimersByTime(2000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(2)
    cleanup()
  })
})

/* ── 判据 17 ── */

describe('17 在途互斥：上一发未回不重复发；done 回来后恢复发', () => {
  it('未回 ⇒ 再走一拍也不发；`done` 跑过之后再走一拍 ⇒ 又发', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    vi.advanceTimersByTime(1000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // 30 秒内连走很多拍：全部被在途互斥挡住
    vi.advanceTimersByTime(10000)
    expect(LAYUI.table.reload, '在途互斥没生效：上一发未回却又发了').toHaveBeenCalledTimes(1)
    // 模拟「这一发回来了」——手动调自动刷新那一发**自己传的** `done`
    const inFlightCall = LAYUI.table.reload.mock.calls[0]![1] as { done?: (...a: unknown[]) => void }
    inFlightCall.done!(undefined, 5, 8)
    vi.advanceTimersByTime(1000)
    expect(LAYUI.table.reload, 'done 回来后应当恢复发').toHaveBeenCalledTimes(2)
    // `done` 顺带把「当前页」更新成 layui 交回的那个（内容与 render 的 done 是同一份）
    expect(lastReload().page).toEqual({ curr: 5 })
    cleanup()
  })

  it('30 秒活性兜底：那一发**死了**（done 永远不回）也允许下一发，不会永久静默', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    vi.advanceTimersByTime(1000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // 恰好 30 秒时**仍在**兜底窗口内（判据是 `> AUTO_REFRESH_ALIVE_MS`，严格大于）⇒ 仍然不发
    vi.advanceTimersByTime(29000)
    expect(LAYUI.table.reload, '恰好 30 秒就放行 ⇒ 兜底窗口比设计的短').toHaveBeenCalledTimes(1)
    // 越过 30 秒后的下一拍 ⇒ 视为那一发已死，允许下一发
    vi.advanceTimersByTime(2000)
    expect(LAYUI.table.reload, '活性兜底没生效：一次死掉的请求会让自动刷新永久静默').toHaveBeenCalledTimes(2)
    cleanup()
  })
})

/* ── 判据 18 ── */

describe('18 页面不可见就暂停；变可见立刻补一发', () => {
  it('`document.hidden = true` ⇒ 跳过这一拍（派发真事件 + 推进定时器）', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    setHidden(true)
    // 派发真事件（⛔ 不是只调内部函数）：隐藏时**不该**有任何补刷
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(5000)
    expect(LAYUI.table.reload, '页面隐藏时仍然在发请求').not.toHaveBeenCalled()
    cleanup()
  })

  it('置回可见并派发 `visibilitychange` ⇒ **立刻补一发**（不等下一拍），且计时重置', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    setHidden(true)
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(3000)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    // 变可见 —— **不需要**推进时钟就该看到一发
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(LAYUI.table.reload, '变可见后没有立刻补刷').toHaveBeenCalledTimes(1)
    // 计时重置：补刷那一发之后再过 1 秒才轮到下一拍
    vi.advanceTimersByTime(999)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    // ⚠️ 补刷那一发也是「自动刷新那一发」（带 done）⇒ 先让它回来，否则下一拍会被在途互斥挡住
    const call = LAYUI.table.reload.mock.calls[0]![1] as { done?: (...a: unknown[]) => void }
    expect(call.done).toBeTypeOf('function')
    call.done!(undefined, 1, 0)
    vi.advanceTimersByTime(1)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(2)
    cleanup()
  })

  it('关闭状态下变可见**不补刷**（没启用就没有这一发）', async () => {
    const { cleanup } = await mountHall()
    setHidden(true)
    document.dispatchEvent(new Event('visibilitychange'))
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(60000)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    cleanup()
  })
})

/* ── 判据 19 ── */

describe('19 选「关闭」⇒ 零 reload + 文案回「关闭」', () => {
  it('先选 1 秒、再选关闭 ⇒ 之后推进多久都不发，按钮文案回到「关闭」', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    expect(autoBtn(root).textContent).toBe('自动刷新频率：1秒')
    // 选「关闭」
    chooseStep(root, 0)
    expect(autoBtn(root).textContent).toBe('自动刷新频率：关闭')
    LAYUI.table.reload.mockClear()
    vi.advanceTimersByTime(10000)
    expect(LAYUI.table.reload, '关了还在刷').not.toHaveBeenCalled()
    // `localStorage` 里也落成 0（下次进来仍是关闭）
    expect(window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY)).toBe('0')
    cleanup()
  })

  it('换档不叠加定时器：1 秒 → 2 秒之后，每 2 秒恰好一发', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    chooseStep(root, 2000)
    // 让每一发都「回来」，好把「有几个定时器在跑」这件事量出来
    const flush = (): void => {
      const calls = LAYUI.table.reload.mock.calls
      const last = calls[calls.length - 1]
      if (last) (last[1] as { done?: (...a: unknown[]) => void }).done?.(undefined, 1, 0)
    }
    vi.advanceTimersByTime(2000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    flush()
    vi.advanceTimersByTime(2000)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(2)
    cleanup()
  })
})

/* ── 判据 20 ── */

describe('20 非法缓存值 ⇒ 挂载后显示「关闭」且零定时器（fail-safe）', () => {
  it.each(['500', 'abc', '-1000'])('localStorage = %s', async (bad) => {
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, bad)
    const { root, cleanup } = await mountHall()
    expect(autoBtn(root).textContent).toBe('自动刷新频率：关闭')
    vi.advanceTimersByTime(60000)
    expect(LAYUI.table.reload, `非法值 ${bad} 竟然起了定时器`).not.toHaveBeenCalled()
    cleanup()
  })

  it('合法缓存值 10000 ⇒ 按钮显示 10秒，并且真的按 10 秒发', async () => {
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, '10000')
    const { root, cleanup } = await mountHall()
    expect(autoBtn(root).textContent).toBe('自动刷新频率：10秒')
    vi.advanceTimersByTime(9999)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('`localStorage` 读写抛异常时当关闭（隐私模式），⛔ 不打白整页', async () => {
    const boom = (): never => {
      throw new Error('SecurityError: 存储被禁用')
    }
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(boom)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(boom)
    try {
      const { root, cleanup } = await mountHall()
      expect(autoBtn(root).textContent).toBe('自动刷新频率：关闭')
      // 选一档也不许抛（写失败只影响「下次还记不记得」）
      expect(() => chooseStep(root, 1000)).not.toThrow()
      expect(autoBtn(root).textContent).toBe('自动刷新频率：1秒')
      cleanup()
    } finally {
      getItem.mockRestore()
      setItem.mockRestore()
    }
  })
})

/* ── 判据 21 ── */

describe('21 卸载后零 reload（定时器与文档级监听都收了）', () => {
  it('调卸载函数后推进时钟 ⇒ 零 reload（且 `clearInterval` 真被调了）', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    const created = [...TIMERS.created]
    expect(created.length, '选了 1 秒却没有起定时器').toBeGreaterThan(0)
    cleanup()
    // 结构性读数（不只是「没发请求」）：那些句柄确实都被清掉了
    // ⚠️ `expect.arrayContaining` 的写法在**空**实际值上会给出误导性的失败信息 ⇒ 逐项断言
    for (const id of created) {
      expect(TIMERS.cleared, `卸载时没有 clearInterval(${String(id)})（定时器泄漏）`).toContain(id)
    }
    vi.advanceTimersByTime(60000)
    expect(LAYUI.table.reload, '卸载后还有定时器在发请求（泄漏）').not.toHaveBeenCalled()
    // 页内那颗按钮也随 `root.innerHTML = ''` 一起没了（没有半新半旧的残留）
    expect(root.querySelector('#autoRefreshBtn')).toBeNull()
  })

  it('卸载后派发 `visibilitychange` 也不补刷（文档级监听已摘）', async () => {
    const { root, cleanup } = await mountHall()
    chooseStep(root, 1000)
    cleanup()
    setHidden(true)
    document.dispatchEvent(new Event('visibilitychange'))
    setHidden(false)
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(10000)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
  })
})
