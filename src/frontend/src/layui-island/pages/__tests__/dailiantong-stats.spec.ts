// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DailiantongStats } from '@/api/dailiantongExtra'

/**
 * 【发平台设置 → 代练通按日统计】island 页的契约测试（dl-D，2026-10-27）。
 *
 * 这一页是**自有功能**（对标物里没有这一页），没有证据件可对拍；本文件钉的是
 * **有真实后果**的约定（`AGENTS.md` §5-G 的「自建 island 页必须有驱动真交互的页测试」）：
 *
 * 1. **挂载即请求**：默认窗口 = 最近 7 天（含今天），GET 载荷恰好 `{start, end, is_pub}`；
 * 2. **字段来自响应**：表头 / 单元格 / 合计行都断言**响应里的具体值**（⛔ 不钉常量）；
 * 3. **三态可见**：加载中 / 空态 / 失败态（后端中文原样上屏 + 重试）；
 * 4. **四条 island 陷阱**：类名与选择器两个常量、以 `(` 开头的语句前置分号（本页没有这种语句，
 *    仍以运行期 DOM 判挂载顺序）、先 `bootstrapIsland` 再塞标记、`initIslandElements` 留在后面。
 *
 * ## 手法（照 `self-registration.spec.ts` / `xgj-receipts.interaction.spec.ts`）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/dailiantongExtra` 跑真实现，
 *   所以「打到哪条 URL、params 里是什么」这两条判据才有意义；
 * - `vi.mock('@/api/auth')`：身份（iframe 深链那道门）必须可控。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：日期 / 数字都是编的，不是任何真租户的数据。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  // ⚠️ 挂载顺序的**运行期**判据（§5-G 陷阱 ③）。探针用查询按钮 id（在 buildHtml 里）。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    seenByBootstrap,
    seenByInit,
    layer: {
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#dltStatsQuery') !== null)
    return { layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#dltStatsQuery') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

/** 只换**网络出口**（见文件头）。`request()` 返回已解包的 `data`。 */
vi.mock('@/api/http', () => ({ request: vi.fn(), CSRF_HEADER_NAME: 'X-CSRFToken' }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import {
  DAILIANTONG_EXTRA_NODE,
  DAILIANTONG_STATS_URL,
  STATS_SIDE_ACCEPT,
  STATS_SIDE_PUBLISH,
} from '@/api/dailiantongExtra'
import { request } from '@/api/http'
import { ROUTE_NODE_CODES } from '@/config/routePermissions'
import page, {
  __testables,
  DATE_FROM_ID,
  DATE_TO_ID,
  MESSAGES,
  PAGE_KEY,
  PANEL_SELECTOR,
  QUERY_BUTTON_ID,
  RETRY_SELECTOR,
  SIDE_SELECT_ID,
  STATUS_ID,
  TABLE_BODY_ID,
  TABLE_HEAD_ID,
} from '../dailiantong-stats'

const { bodyHtml, buildHtml, columnsFor, defaultStatsRange, statValue } = __testables

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

/** 一次 HTTP 请求的入参（我们只关心这三个）。 */
type Cfg = { method?: string; url?: string; params?: unknown; data?: unknown }

/** 身份夹具：持 `order.list` 的**非管理员**（管理员会短路放行，钉不住节点门）。 */
const KEFU = {
  id: 7,
  username: 'kefu-a',
  display_name: '客服甲',
  identity_plane: 'tenant_a',
  is_tenant_admin: false,
  roles: [] as string[],
  permissions: [] as string[],
  menu_permissions: [DAILIANTONG_EXTRA_NODE],
}

/** 合成统计回包（键名照上游 `LevelOrderStat` 文档；值全是编的）。 */
function statsFixture(over: Partial<DailiantongStats> = {}): DailiantongStats {
  return {
    items: [
      {
        QDate: '2026-10-20',
        PubCount: '7',
        SettleCount: '5',
        SettleRate: '71.43',
        QAvgTime: '3.5小时',
        PubBal: '140.00',
      },
      {
        QDate: '2026-10-21',
        PubCount: '3',
        SettleCount: '3',
        SettleRate: '100',
        QAvgTime: '2小时',
        PubBal: '60.00',
      },
    ],
    total: { PubCount: '10', SettleCount: '8', SettleRate: '80', QAvgTime: '2.75小时', PubBal: '200.00' },
    record_count: 2,
    start: '2026-10-21',
    end: '2026-10-27',
    is_pub: STATS_SIDE_PUBLISH,
    ...over,
  }
}

function flush(rounds = 6): Promise<void> {
  return (async () => {
    for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  })()
}

/** 按 `METHOD URL` 造一次响应路由；**没有备好的响应就抛**（⛔ 不是静默回 undefined）。 */
function respond(routes: Record<string, unknown>): void {
  mockedRequest.mockImplementation((config: Cfg) => {
    const key = `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`
    if (!(key in routes)) throw new Error(`用例没有备 ${key} 的响应`)
    const value = routes[key]
    // 失败那一支给**函数**：`Promise.reject(...)` 写在字面量里会在装配期建出未处理的 rejection。
    if (typeof value === 'function') return (value as () => Promise<never>)()
    return Promise.resolve(value)
  })
}

function requests(): string[] {
  return mockedRequest.mock.calls.map((call) => {
    const cfg = call[0] as Cfg
    return `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url}`
  })
}

function lastConfig(): Cfg {
  const calls = mockedRequest.mock.calls
  const call = calls[calls.length - 1]
  if (!call) throw new Error('还没有发过请求')
  return call[0] as Cfg
}

let root: HTMLElement

function click(el: HTMLElement): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
}

function statusText(): string {
  return root.querySelector<HTMLElement>(`#${STATUS_ID}`)?.textContent ?? ''
}

beforeEach(() => {
  vi.clearAllMocks()
  LAYUI.messages.length = 0
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  document.body.innerHTML = ''
  root = document.createElement('div')
  document.body.appendChild(root)
  mockedFetchMe.mockResolvedValue(KEFU)
  respond({ [`GET ${DAILIANTONG_STATS_URL}`]: statsFixture() })
})

afterEach(() => {
  document.body.innerHTML = ''
})

async function mount(): Promise<void> {
  await page.mount(root, { onNavigate: () => {} })
  await flush()
}

describe('挂载：三条 island 陷阱、默认窗口、挂载即请求', () => {
  it('先 bootstrapIsland、再塞标记、最后 initIslandElements（运行期 DOM 判，不是读源码）', async () => {
    await mount()
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
  })

  it('① 类名与选择器是两个常量（`class=".x"` 会永远匹配不到）', () => {
    const host = document.createElement('div')
    host.innerHTML = buildHtml({ start: '2026-10-21', end: '2026-10-27' })
    expect(host.querySelector(PANEL_SELECTOR)).not.toBeNull()
    expect(host.innerHTML).not.toContain('class=".')
  })

  it('节点码与 `config/routePermissions.ts` 的登记同值（iframe 自检那道门不另开一套判据）', () => {
    expect(DAILIANTONG_EXTRA_NODE).toBe('order.list')
    expect(ROUTE_NODE_CODES[PAGE_KEY]).toBe(DAILIANTONG_EXTRA_NODE)
  })

  it('默认窗口 = 最近 7 天（含今天）；GET 载荷恰好 `{start, end, is_pub}`', async () => {
    const expected = defaultStatsRange()
    await mount()
    expect(requests()).toEqual([`GET ${DAILIANTONG_STATS_URL}`])
    expect(lastConfig().method).toBe('GET')
    expect(lastConfig().params).toEqual({
      start: expected.start,
      end: expected.end,
      is_pub: STATS_SIDE_PUBLISH,
    })
    // 日期框里显示的就是真正发出去的那一对值（⛔ 不靠后端默认值）。
    expect(root.querySelector<HTMLInputElement>(`#${DATE_FROM_ID}`)?.value).toBe(expected.start)
    expect(root.querySelector<HTMLInputElement>(`#${DATE_TO_ID}`)?.value).toBe(expected.end)
  })

  it('defaultStatsRange(指定日) 算出 7 天窗口（纯函数判据，不依赖系统时钟）', () => {
    expect(defaultStatsRange(new Date(2026, 9, 27, 12, 0, 0))).toEqual({
      start: '2026-10-21',
      end: '2026-10-27',
    })
  })

  it('没有节点时**不注入标记、不发业务请求**（深链自检；与路由那道门同源）', async () => {
    mockedFetchMe.mockResolvedValue({ ...KEFU, menu_permissions: [] })
    await page.mount(root, { onNavigate: () => {} })
    await flush()
    expect(LAYUI.messages).toContain(MESSAGES.noAccess)
    expect(root.querySelector(PANEL_SELECTOR)).toBeNull()
    expect(requests()).toEqual([])
  })
})

describe('三态：加载中 / 空 / 失败（⛔ 不静默空白）', () => {
  it('请求还没回来时状态条写着「加载中…」', async () => {
    let resolveStats!: (value: DailiantongStats) => void
    mockedRequest.mockImplementation(
      () =>
        new Promise<DailiantongStats>((resolve) => {
          resolveStats = resolve
        }),
    )
    const mounting = page.mount(root, { onNavigate: () => {} })
    await flush(4)
    expect(statusText()).toBe(MESSAGES.loading)
    // 表头已经在了（加载中不是一张什么都没有的白板）。
    expect(root.querySelector<HTMLElement>(`#${TABLE_HEAD_ID}`)?.textContent).toContain('日期')
    resolveStats(statsFixture())
    await mounting
    await flush(2)
    expect(root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)?.textContent).toContain('71.43')
  })

  it('空结果 ⇒ 状态条说「这个区间里还没有统计数据」，表头仍在', async () => {
    respond({
      [`GET ${DAILIANTONG_STATS_URL}`]: statsFixture({
        items: [],
        total: {},
        record_count: 0,
        start: '2026-10-21',
        end: '2026-10-27',
      }),
    })
    await mount()
    expect(statusText()).toContain(MESSAGES.empty)
    expect(root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)?.textContent).toContain(MESSAGES.empty)
    expect(root.querySelector<HTMLElement>(`#${TABLE_HEAD_ID}`)?.textContent).toContain('发单量')
  })

  it('失败 ⇒ 后端中文**原样**上屏 + 一颗「重试」；点重试成功后铺出数据', async () => {
    const failure = '读取代练通按日统计失败（上游提示：账号未配置）。'
    respond({
      [`GET ${DAILIANTONG_STATS_URL}`]: () => Promise.reject(new Error(failure)),
    })
    await mount()
    expect(statusText()).toContain(`${MESSAGES.failed}：${failure}`)
    expect(root.querySelector(RETRY_SELECTOR)).not.toBeNull()

    respond({ [`GET ${DAILIANTONG_STATS_URL}`]: statsFixture() })
    click(root.querySelector<HTMLElement>(RETRY_SELECTOR)!)
    await flush()
    expect(requests()).toHaveLength(2)
    expect(statusText()).toContain('共 2 天数据')
    expect(root.querySelector<HTMLElement>(`#${TABLE_BODY_ID}`)?.textContent).toContain('100')
  })
})

describe('字段来自响应（断言具体值，不钉常量）', () => {
  it('表头按文档顺序只出现返回的键；单元格是响应里的原值', async () => {
    await mount()
    const heads = [...root.querySelectorAll(`#${TABLE_HEAD_ID} th`)].map((th) => th.textContent)
    expect(heads).toEqual(['日期', '发单量', '发单金额', '完单量', '均单验收时间', '完单率'])
    const firstRow = root.querySelectorAll(`#${TABLE_BODY_ID} tr`)[0]!
    const cells = [...firstRow.querySelectorAll('td')].map((td) => td.textContent)
    expect(cells).toEqual(['2026-10-20', '7', '140.00', '5', '3.5小时', '71.43'])
  })

  it('有 `total` 时多一行「合计」，值来自响应的 `total`', async () => {
    await mount()
    const rows = [...root.querySelectorAll(`#${TABLE_BODY_ID} tr`)]
    expect(rows).toHaveLength(3)
    const totalCells = [...rows[2]!.querySelectorAll('td')].map((td) => td.textContent)
    expect(totalCells).toEqual(['合计', '10', '200.00', '8', '2.75小时', '80'])
  })

  it('回包里多出来的键追加在末尾（表头就是原始键名，⛔ 不猜中文）', () => {
    const columns = columnsFor([{ QDate: '2026-10-20', FooBar: '1' }], {})
    expect(columns.map((column) => column.key)).toEqual(['QDate', 'FooBar'])
    expect(columns[columns.length - 1]?.label).toBe('FooBar')
  })

  it('缺失 / 空白值显示「—」（空结果 ≠ 0）', () => {
    expect(statValue(null)).toBe('—')
    expect(statValue(undefined)).toBe('—')
    expect(statValue('  ')).toBe('—')
    expect(statValue('0')).toBe('0')
  })

  it('空结果时 `columnsFor` 退回全部已知列（否则空态没有表头）', () => {
    expect(columnsFor([], {}).length).toBeGreaterThan(1)
    expect(columnsFor([], {})[0]?.label).toBe('日期')
  })
})

describe('筛选：方向 / 日期预检', () => {
  it('切到「接单」再查询 ⇒ is_pub=0', async () => {
    await mount()
    root.querySelector<HTMLSelectElement>(`#${SIDE_SELECT_ID}`)!.value = '0'
    click(root.querySelector<HTMLElement>(`#${QUERY_BUTTON_ID}`)!)
    await flush()
    expect(lastConfig().params).toMatchObject({ is_pub: STATS_SIDE_ACCEPT })
  })

  it('开始日期晚于结束日期 ⇒ 就地提示且**不发请求**', async () => {
    await mount()
    root.querySelector<HTMLInputElement>(`#${DATE_FROM_ID}`)!.value = '2026-10-27'
    root.querySelector<HTMLInputElement>(`#${DATE_TO_ID}`)!.value = '2026-10-20'
    click(root.querySelector<HTMLElement>(`#${QUERY_BUTTON_ID}`)!)
    await flush()
    expect(requests()).toHaveLength(1)
    expect(statusText()).toContain(MESSAGES.badRange)
  })

  it('日期留空 ⇒ 就地提示且**不发请求**', async () => {
    await mount()
    root.querySelector<HTMLInputElement>(`#${DATE_FROM_ID}`)!.value = ''
    click(root.querySelector<HTMLElement>(`#${QUERY_BUTTON_ID}`)!)
    await flush()
    expect(requests()).toHaveLength(1)
    expect(statusText()).toContain(MESSAGES.needDates)
  })
})

describe('纯函数自证（能失败）', () => {
  it('bodyHtml 空列表给的是空态那一行，不是空白', () => {
    const html = bodyHtml(
      statsFixture({ items: [], total: {}, record_count: 0 }),
    )
    expect(html).toContain(MESSAGES.empty)
    expect(html).toContain('colspan=')
  })

  it('statValue 不把数字 0 当成缺失（边界判据用 is not None）', () => {
    expect(statValue(0)).toBe('0')
  })
})
