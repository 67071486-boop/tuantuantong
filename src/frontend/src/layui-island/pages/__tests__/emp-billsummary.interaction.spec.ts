// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【结算记录】页（D-0319 按天聚合重设计，2026-10-28）的**交互**测试——驱动**真 DOM 事件**。
 *
 * 与 `emp-billsummary.spec.ts`（声明 / 纯函数 / 挂载行为）的分工：本文件钉的是
 * 「真用户操作那条链」——真点击 / 真键盘事件流经页面自己的监听器，以及
 * `AGENTS.md` §5-G 的三条 island 陷阱的**运行期**判据（E-0061 / E-0067）：
 *
 * 1. ⭐ **陷阱 ③ 的顺序**：`bootstrapIsland` 时页面标记还**没**进 DOM、
 *    `initIslandElements` 时**已经**进 DOM（先挂样式再塞标记、init 在塞标记之后）。
 * 2. ⭐ **陷阱 ①**：类名与选择器两个常量各司其职——页面**用选择器**找到节点、
 *    标记里拼的是**类名**（真交互链上两边都被真用上）。
 * 3. ⭐ **真交互**：点真按钮（`#searchBtn` / `#detailSearchBtn`）、点行内真链接
 *    （`a.viewbilldetail` / `a.orderdetails`——用**页面自己的 templet** 生成 layui 会插的
 *    那串标记）、真键盘 Enter——事件流经 `addEventListener` 注册的监听器，不是直接调函数。
 *
 * ⚠️ 本页**没有定时器**（E-0198 的假定时器规矩不适用；导出落盘后那句
 * `setTimeout(revokeObjectURL, 0)` 是一次性收尾，不是页面的周期任务）。
 * ⚠️ 夹具一律**合成值**（E-0077）。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  const renders: unknown[] = []
  const reloads: unknown[][] = []
  const layerOpens: unknown[] = []
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    messages, seenByBootstrap, seenByInit, renders, reloads, layerOpens, tableHandlers,
    table: {
      render: (o: unknown) => renders.push(o),
      reload: (id: unknown, o: unknown) => reloads.push([id, o]),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: {
      open: (o: unknown) => layerOpens.push(o),
      msg: (t: string) => messages.push(t),
    },
    laydate: { render: () => {} },
    laypage: { render: () => {} },
    element: { init: () => {} },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    // 陷阱 ③ 的运行期判据：bootstrap 那一刻，页面标记还**没**进 DOM（false）。
    LAYUI.seenByBootstrap.push(document.querySelector('.billsummaryPanel') !== null)
    return LAYUI
  }),
  initIslandElements: vi.fn(() => {
    // 陷阱 ③ 的运行期判据：init 那一刻，页面标记已经在 DOM 里。
    LAYUI.seenByInit.push(document.querySelector('.billsummaryPanel') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))

/** 只替 `downloadPerformanceExport`（导出是真下载，不走 `request()`）。 */
vi.mock('@/api/performances', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/performances')>()
  return {
    ...actual,
    downloadPerformanceExport: vi.fn(async () => ({
      blob: new Blob(['PK fake']),
      filename: 'performances-20261028.xlsx',
    })),
  }
})

import { request } from '@/api/http'
import type { SettlementDailyItem, SettlementListItem, SettlementSourceItem } from '@/api/settlements'
import { downloadPerformanceExport } from '@/api/performances'
import page, {
  AMOUNT_TOGGLE_SELECTOR,
  buildDailyCols,
  buildDetailCols,
  DETAIL_TITLE_SELECTOR,
  PANEL_SELECTOR,
  SOURCE_RETRY_SELECTOR,
  SOURCE_VIEW_SELECTOR,
  TABLE_ID,
  TABLE2_ID,
  VIEW_DETAIL_SELECTOR,
} from '../emp-billsummary'

const mockedRequest = vi.mocked(request)

function dayItem(over: Partial<SettlementDailyItem>): SettlementDailyItem {
  return { date: '2026-10-28', order_count: 2, total_amount: '20.00', ...over }
}

/** 来源分解的合成行：一个指定来源 + 一个「未指定」（`null`/`null`）。 */
function sourceItem(over: Partial<SettlementSourceItem>): SettlementSourceItem {
  return { source_id: 5, source_name: '甲店铺', order_count: 1, total_amount: '15.00', ...over }
}

const SOURCES: SettlementSourceItem[] = [
  sourceItem({}),
  sourceItem({ source_id: null, source_name: null, order_count: 1, total_amount: '5.00' }),
]

function detailRow(over: Partial<SettlementListItem>): SettlementListItem {
  return {
    id: 1, order_no: '20260920213215233029', source_order_no: 'TB-合成-结算-来源甲',
    user: 7, username: '小李', display_name: '合成小李', emp_type: '打单工资',
    temp_amount: '12.00', bonus_amount: '12.00', deduct_mode: '', deduct_value: '0.00',
    taker_price: null, review_status: '已通过', settle_status: '已结账',
    created_at: '2026-09-20T21:32:15+08:00', payable_net: '12.00', settled_amount: '12.00',
    project: '深渊满星', settled_at: '2026-10-28T21:00:00+08:00', settled_by: '财务甲',
    ...over,
  } as SettlementListItem
}

/** 默认后端：sources 分解回两行、daily-summary 回一天、/settlements 回一行（参数原样可断言）。 */
mockedRequest.mockImplementation(async (config: { url?: string }) => {
  const url = String(config.url)
  // ⚠️ sources 分支必须排在 daily-summary 之前判（前者的 URL 也 includes 'daily-summary'）。
  if (url.includes('/settlements/daily-summary/sources')) {
    return { items: SOURCES }
  }
  if (url.includes('/settlements/daily-summary')) {
    return { items: [dayItem({})], total: 1, page: 1, page_size: 20 }
  }
  return { items: [detailRow({})], total: 1, page: 1, page_size: 200 }
})

/** 微任务冲刷（挂载 / 取数链上的 await 都要落地）。 */
async function flush(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

/** 把「layui 会插进 DOM 的那串」用页面自己的 templet 生成，作为真节点塞进去。 */
function anchorFrom(templet: (r: never) => string, row: unknown): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = templet(row as never)
  return host.firstElementChild as HTMLElement
}

describe('结算记录页（按天聚合）的真交互', () => {
  let root: HTMLElement
  let unmount: () => void

  beforeEach(async () => {
    vi.clearAllMocks()
    for (const bucket of [LAYUI.messages, LAYUI.seenByBootstrap, LAYUI.seenByInit, LAYUI.renders, LAYUI.layerOpens]) {
      bucket.length = 0
    }
    LAYUI.reloads.length = 0
    LAYUI.tableHandlers.clear()
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    unmount = await page.mount(root, { onNavigate: vi.fn() })
    await flush()
  })

  afterEach(() => {
    unmount?.()
    document.body.innerHTML = ''
  })

  it('陷阱 ③：bootstrap 时标记未进 DOM、initIslandElements 时已进（运行期判据）', () => {
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('挂载即取数并渲染主表（真 mount 链路：daily-summary → table.render）', () => {
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    const opts = LAYUI.renders[0] as { elem?: string; data?: unknown[] }
    expect(opts.elem).toBe(`#${TABLE_ID}`)
    expect(opts.data).toEqual([{ kind: 'day', day: dayItem({}) }])
  })

  it('点行内「查看明细」（真点击）⇒ 按那天取 /settlements 并渲染明细 + 写标题', async () => {
    const link = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({ date: '2026-10-27' }) })
    root.appendChild(link)
    link.click()
    await flush()
    const detailCalls = mockedRequest.mock.calls.filter(([c]) =>
      String((c as { url?: string }).url) === '/api/business/settlements',
    )
    expect(detailCalls).toHaveLength(1)
    expect((detailCalls[0]![0] as { params?: Record<string, unknown> }).params).toMatchObject({
      date_from: '2026-10-27', date_to: '2026-10-27', settle_status: '已结账',
    })
    const detail = LAYUI.renders[1] as { elem?: string }
    expect(detail.elem).toBe(`#${TABLE2_ID}`)
    expect(root.querySelector(DETAIL_TITLE_SELECTOR)!.textContent).toBe('2026-10-27的结算明细')
  })

  it('点明细里「关联订单」（真点击）⇒ 原地开层（layer.open，参数带 20 位单号）', async () => {
    const viewLink = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({}) })
    root.appendChild(viewLink)
    viewLink.click()
    await flush()
    const orderLink = anchorFrom(buildDetailCols()[3]!.templet!, detailRow({}))
    root.appendChild(orderLink)
    orderLink.click()
    await flush()
    expect(LAYUI.layerOpens).toHaveLength(1)
    const opts = LAYUI.layerOpens[0] as { content?: string; area?: unknown }
    expect(opts.content).toContain('page=order-orderview&order_no=20260920213215233029')
    expect(opts.area).toEqual(['1125px', '90%'])
  })

  it('点「总结算金额」格（真点击）⇒ 拉来源分解并 reload 出子行；再点收起（D-0323）', async () => {
    const toggle = anchorFrom(buildDailyCols(() => false)[2]!.templet!, {
      kind: 'day', day: dayItem({ date: '2026-10-28' }),
    })
    root.appendChild(toggle)
    expect(root.querySelector(AMOUNT_TOGGLE_SELECTOR)).toBe(toggle) // 陷阱 ①：真选择器命中
    toggle.click()
    await flush()
    const srcCalls = mockedRequest.mock.calls.filter(([c]) =>
      String((c as { url?: string }).url).includes('/settlements/daily-summary/sources'),
    )
    expect(srcCalls).toHaveLength(1)
    expect((srcCalls[0]![0] as { params?: Record<string, unknown> }).params).toEqual({ date: '2026-10-28' })
    const expanded = LAYUI.reloads[LAYUI.reloads.length - 1] as [unknown, { data?: { kind?: string }[] }]
    expect(expanded[0]).toBe(TABLE_ID)
    expect(expanded[1]!.data!.filter((r) => r.kind === 'source')).toHaveLength(2)
    // 再点一次 = 收起：子行撤掉（缓存保留——不发第二次 sources 请求）。
    toggle.click()
    await flush()
    const collapsed = LAYUI.reloads[LAYUI.reloads.length - 1] as [unknown, { data?: { kind?: string }[] }]
    expect(collapsed[1]!.data!.every((r) => r.kind === 'day')).toBe(true)
    expect(mockedRequest.mock.calls.filter(([c]) =>
      String((c as { url?: string }).url).includes('/settlements/daily-summary/sources'),
    )).toHaveLength(1)
  })

  it('点来源子行「查看明细」（真点击）⇒ /settlements 带 order_source=5、标题带来源上下文（D-0323）', async () => {
    const toggle = anchorFrom(buildDailyCols(() => false)[2]!.templet!, {
      kind: 'day', day: dayItem({ date: '2026-10-28' }),
    })
    root.appendChild(toggle)
    toggle.click()
    await flush()
    const srcLink = anchorFrom(buildDailyCols()[3]!.templet!, {
      kind: 'source', date: '2026-10-28', item: SOURCES[0]!,
    })
    root.appendChild(srcLink)
    expect(root.querySelector(SOURCE_VIEW_SELECTOR)).toBe(srcLink) // 陷阱 ①
    srcLink.click()
    await flush()
    const detailCalls = mockedRequest.mock.calls
      .map(([c]) => c as { url?: string; params?: Record<string, unknown> })
      .filter((c) => c.url === '/api/business/settlements')
    expect(detailCalls[detailCalls.length - 1]!.params).toMatchObject({
      date_from: '2026-10-28', date_to: '2026-10-28', settle_status: '已结账', order_source: 5,
    })
    expect(root.querySelector(DETAIL_TITLE_SELECTOR)!.textContent).toBe('2026-10-28的结算明细（来源：甲店铺）')
  })

  it('点来源失败行「重试」（真点击）⇒ 日期形状闸：坏形状零请求、好形状照发（F-03）', async () => {
    // 用页面自己的 error 行 templet 造「重试」链接（与真实渲染同一串标记）。
    const srcCalls = () => mockedRequest.mock.calls.filter(([c]) =>
      String((c as { url?: string }).url).includes('/settlements/daily-summary/sources'),
    )
    const badRetry = anchorFrom(buildDailyCols()[3]!.templet!, {
      kind: 'source-error', date: '20261028（templet 造不出的形状）',
    })
    root.appendChild(badRetry)
    expect(root.querySelector(SOURCE_RETRY_SELECTOR)).toBe(badRetry) // 陷阱 ①：真选择器命中
    badRetry.click()
    await flush()
    expect(srcCalls(), '形状不认识的 date 不许发请求').toHaveLength(0)
    // 对照腿：同一个 templet、形状合法 ⇒ 照发（闸不是恒拦——两腿缺一不可）。
    const goodRetry = anchorFrom(buildDailyCols()[3]!.templet!, {
      kind: 'source-error', date: '2026-10-27',
    })
    root.appendChild(goodRetry)
    goodRetry.click()
    await flush()
    expect(srcCalls()).toHaveLength(1)
    expect((srcCalls()[0]![0] as { params?: Record<string, unknown> }).params).toEqual({
      date: '2026-10-27',
    })
  })

  it('正看着 A 天明细时重看同一天取数失败 ⇒ 只弹错、明细卡保留（F-03）', async () => {
    // 先成功打开 A 天明细。
    const viewLink = anchorFrom(buildDailyCols()[3]!.templet!, {
      kind: 'day', day: dayItem({ date: '2026-10-28' }),
    })
    root.appendChild(viewLink)
    viewLink.click()
    await flush()
    expect(root.querySelector(DETAIL_TITLE_SELECTOR)!.textContent).toBe('2026-10-28的结算明细')
    // 单号框塞个值（resetDetail 会清它 ⇒ 值还在 = 现场真的没被动）。
    ;(root.querySelector<HTMLInputElement>('#key1')!).value = '20260920213215233029'
    // 重看同一天：下一次取数失败（真点击、真请求失败）。
    mockedRequest.mockImplementationOnce(async () => {
      throw new Error('后端冒烟了')
    })
    viewLink.click()
    await flush()
    // 错误看得见（不是「点了没反应」），文案是后端 msg 原文（不自造）。
    expect(LAYUI.messages).toContain('后端冒烟了')
    // 明细卡**保留**：标题还在、明细表没有被清成空表、单号框原值还在。
    expect(root.querySelector(DETAIL_TITLE_SELECTOR)!.textContent).toBe('2026-10-28的结算明细')
    expect(LAYUI.reloads.some(([id, o]) =>
      id === TABLE2_ID && (o as { data?: unknown[] }).data?.length === 0,
    )).toBe(false)
    expect((root.querySelector<HTMLInputElement>('#key1')!).value).toBe('20260920213215233029')
  })

  it('点「搜索」按钮（真点击）⇒ 明细卡还原 + 主表带新日期回第 1 页', async () => {
    // 先打开一天明细（搜索必须能把它还原）。
    const viewLink = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({}) })
    root.appendChild(viewLink)
    viewLink.click()
    await flush()
    expect(root.querySelector(DETAIL_TITLE_SELECTOR)!.textContent).not.toBe('')
    ;(root.querySelector<HTMLInputElement>('#sDate')!).value = '2026-10-01 00:00:00'
    root.querySelector<HTMLElement>('#searchBtn')!.click()
    await flush()
    const calls = mockedRequest.mock.calls.map(([c]) => c as { url?: string; params?: Record<string, unknown> })
    // ⚠️ 不用 `.at(-1)`：tsconfig 的 lib 目标不含 es2022（TS2550，同仓先例见
    // `order-screenshots-upload.spec.ts` 的注释）。
    const last = calls[calls.length - 1]!
    expect(last.url).toContain('daily-summary')
    expect(last.params).toMatchObject({ date_from: '2026-10-01', page: 1 })
    expect(root.querySelector(DETAIL_TITLE_SELECTOR)!.textContent).toBe('')
    expect(LAYUI.reloads.some(([id, o]) => id === TABLE2_ID && (o as { data?: unknown[] }).data?.length === 0)).toBe(true)
  })

  it('在订单号框上按 Enter（真键盘事件）⇒ 搜明细：没选天先挡（提示 + 零明细请求）', async () => {
    const input = root.querySelector<HTMLInputElement>('#key1')!
    input.value = '20260920213215233029'
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flush()
    expect(LAYUI.messages).toContain('请先在上方列表点击「查看明细」选择日期')
    expect(mockedRequest.mock.calls.filter(([c]) => String((c as { url?: string }).url) === '/api/business/settlements')).toHaveLength(0)
  })

  it('选天后再按 Enter（真键盘事件）⇒ 带 order_no 重取明细', async () => {
    const viewLink = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({ date: '2026-10-28' }) })
    root.appendChild(viewLink)
    viewLink.click()
    await flush()
    const input = root.querySelector<HTMLInputElement>('#key1')!
    input.value = '20260920213215233029'
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flush()
    const detailCalls = mockedRequest.mock.calls
      .map(([c]) => c as { url?: string; params?: Record<string, unknown> })
      .filter((c) => c.url === '/api/business/settlements')
    expect(detailCalls[detailCalls.length - 1]!.params).toMatchObject({
      order_no: '20260920213215233029', date_from: '2026-10-28', date_to: '2026-10-28',
    })
  })

  it('点「导出记录」（toolbar 回调，layui 事件）⇒ 导出当前天（date_type=settled）并落文件', async () => {
    const viewLink = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({ date: '2026-10-26' }) })
    root.appendChild(viewLink)
    viewLink.click()
    await flush()
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL: vi.fn(),
    }))
    const handler = LAYUI.tableHandlers.get(`toolbar(${TABLE2_ID})`) as (o: { event?: string }) => void
    expect(handler, '明细表的 toolbar 没有注册').toBeDefined()
    handler({ event: 'exportBtn' })
    await flush()
    expect(vi.mocked(downloadPerformanceExport)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(downloadPerformanceExport).mock.calls[0]![0]).toMatchObject({
      date_type: 'settled', date_from: '2026-10-26', date_to: '2026-10-26', settle_status: '已结账',
    })
    expect(URL.createObjectURL).toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('陷阱 ①（真交互链上的判据）：页面用选择器找节点、标记里拼类名', () => {
    // 标记里是类名（无选择器形态）。
    expect(root.innerHTML).not.toContain('class=".')
    // 而页面自己的行内链接（templet 产物）能被**选择器**命中——真交互链上两边各司其职。
    const link = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({}) })
    root.appendChild(link)
    expect(root.querySelector(VIEW_DETAIL_SELECTOR)).toBe(link)
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
  })

  it('卸载后再点「查看明细」⇒ 零新请求（监听真的被移除）', async () => {
    unmount()
    const link = anchorFrom(buildDailyCols()[3]!.templet!, { kind: 'day', day: dayItem({}) })
    root.appendChild(link)
    const before = mockedRequest.mock.calls.length
    link.click()
    await flush()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })
})
