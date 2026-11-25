// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BizError } from '@/api/errors'
import {
  XGJ_RECEIPT_LIST_URL,
  XGJ_RECEIPT_NODE,
  XGJ_RECEIPT_SUMMARY_URL,
  type ReceiptDetailRow,
  type ReceiptSummary,
  type ReceiptSummaryRow,
} from '@/api/xgjReceipts'

/**
 * 【财务相关 → 闲管家收款明细】island 页的**交互**测试（2026-10-22，收款明细线）。
 *
 * 这一页是**自有功能**（对标物不接闲管家推送、没有这一页），本文件钉的是**有真实后果**的约定：
 *
 * 1. ⭐ **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有一条能红的交互用例）：
 *    挂载 → 缺省窗口（前一天..前一天）的汇总上屏 + 合计行；点一家店铺 → 真发
 *    `GET …/?shop_identity=…` 明细并重绘（spec §12 F4-D 的参数名）；改日期 → 真发新区间并
 *    **重置**明细区；点「未识别店铺」桶 → 空串参数照样发。
 * 2. **三条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量；以 `(` 开头的语句前置分号；
 *    挂载顺序以**运行期 DOM** 判断（`bootstrapIsland` 看不到标记、`initIslandElements` 看得到）。
 * 3. ⭐ **权限门 fail-closed**：拿不到 `finance.xgj_receipt_detail` ⇒ 一句提示 +
 *    **不注入标记** + **不发任何业务请求**（`fetchMe()` 那一次身份请求照发——它就是这道门）。
 * 4. ⭐ **金额一律字符串上屏**（缺失笔数与金额分开显示）；合计**读后端的 `totals`**
 *    （页面自己不做金额加法）。
 * 5. ⭐ 统计口径声明上屏（spec §10-4：交易成功口径 ≠ 净收款口径，防误读）。
 *
 * ## 手法（照 `agents-wallet-adjust.interaction.spec.ts`，⛔ 不自己发明一套）
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/xgjReceipts` 跑的是**真实现**；
 * - 服务端响应是**活的**：改日期后的重查真的换数据。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：店铺名 / 单号 / 时间戳都是编的。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const renders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const handlers: Record<string, (obj: unknown) => void> = {}
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    renders,
    reloads,
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
      msg: vi.fn((text: string) => {
        messages.push(text)
      }),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#xgjReceiptSummaryTable') !== null)
    return { table: LAYUI.table, layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#xgjReceiptSummaryTable') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import page, {
  DATE_FROM_ID,
  DATE_TO_ID,
  DETAIL_TABLE_ID,
  DETAIL_TITLE_ID,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  PANEL_CLASS,
  SOURCE_GROUP_ID,
  SUMMARY_TABLE_ID,
  TOTALS_ID,
  UNKNOWN_SHOP_LABEL,
  shopDisplayName,
  summaryTableData,
  totalsHtml,
  yesterdayStr,
} from '../xgj-receipts'

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

type Cfg = { method?: string; url?: string; data?: unknown; params?: Record<string, unknown> }

/** 一家店（**合成值**；`shop_identity` 仿详情 `seller_eid` 密文串形状，spec §12 F2-D）。 */
function summaryRow(over: Partial<ReceiptSummaryRow> & { shop_identity: string }): ReceiptSummaryRow {
  return {
    shop_name: `合成店铺${over.shop_identity}`,
    count: 3,
    amount: '123.45',
    missing_count: 1,
    ...over,
  }
}

function detailRow(over: Partial<ReceiptDetailRow> & { id: number }): ReceiptDetailRow {
  return {
    order_no: `22000000000000000${String(over.id).padStart(2, '0')}`,
    seller_id: '660001',
    shop_identity: 'EID-11001',
    shop_name: '合成店铺甲',
    user_name: '合成买家甲',
    amount: '45.60',
    amount_missing: false,
    success_time: '2026-10-21T13:05:00+08:00',
    matched_count: 1,
    match_summary: '命中 1 张：已改已结款 1 张',
    ...over,
  }
}

function summaryPayload(items: ReceiptSummaryRow[]): ReceiptSummary {
  return {
    items,
    totals: {
      shop_count: items.length,
      count: items.reduce((sum, row) => sum + row.count, 0),
      amount: items
        .reduce((sum, row) => sum + Number(row.amount), 0)
        .toFixed(2),
      missing_count: items.reduce((sum, row) => sum + row.missing_count, 0),
    },
  }
}

const SHOP_A = summaryRow({ shop_identity: 'EID-11001', shop_name: '合成店铺甲', count: 3, amount: '100.00', missing_count: 0 })
const SHOP_B = summaryRow({ shop_identity: 'EID-11002', shop_name: '合成店铺乙', count: 1, amount: '0.00', missing_count: 1 })
/** 未识别桶（`shop_identity` 空串；后端名字也没兜出来 ⇒ 前端兜「未识别店铺」，F2-D/F4-D）。 */
const SHOP_UNKNOWN = summaryRow({ shop_identity: '', shop_name: '', count: 2, amount: '7.00', missing_count: 2 })
const DETAIL_A1 = detailRow({ id: 1 })
const DETAIL_A2 = detailRow({ id: 2, amount: '', amount_missing: true })
const DETAIL_UNKNOWN = detailRow({ id: 3, order_no: '2200000000000000003', shop_identity: '', seller_id: '660002' })

let summaryOnServer: ReceiptSummary
let detailOnServer: ReceiptDetailRow[]

function flush(): Promise<void> {
  return (async () => {
    for (let i = 0; i < 8; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

function callTo(method: string, url: string): Cfg {
  const found = mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method && c.url === url)
  if (found.length === 0) throw new Error(`没有发出 ${method.toUpperCase()} ${url}`)
  return found[found.length - 1]!
}

function callCount(method: string, url?: string): number {
  return mockedRequest.mock.calls
    .map((c) => c[0] as Cfg)
    .filter((c) => (c.method ?? 'get').toLowerCase() === method)
    .filter((c) => (url === undefined ? true : c.url === url))
    .length
}

function tableRows(id: string): Array<Record<string, unknown>> {
  const last = [...LAYUI.reloads].reverse().find((r) => r.id === id)
  if (!last) throw new Error(`没有对 ${id} 调用过 reloadData`)
  return last.data as Array<Record<string, unknown>>
}

function clickRow(tableId: string, row: unknown): void {
  LAYUI.handlers[`row(${tableId})`]!({ data: row })
}

function userWithoutNode() {
  return {
    id: 2,
    username: 'synthetic-no-node',
    display_name: '合成无权限',
    identity_plane: 'tenant',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [],
    menu_permissions: [],
    permissions: [],
  }
}

describe('闲管家收款明细 island 页（xgj-receipts）', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    for (const bucket of [
      LAYUI.messages, LAYUI.renders, LAYUI.reloads, LAYUI.seenByBootstrap, LAYUI.seenByInit,
    ]) bucket.length = 0
    for (const key of Object.keys(LAYUI.handlers)) delete LAYUI.handlers[key]
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)

    mockedFetchMe.mockResolvedValue({
      id: 1,
      username: 'synthetic-finance',
      display_name: '合成财务',
      identity_plane: 'tenant',
      is_tenant_admin: false,
      is_account_admin: false,
      roles: [],
      menu_permissions: [XGJ_RECEIPT_NODE],
      permissions: [],
    })

    summaryOnServer = summaryPayload([SHOP_A, SHOP_B])
    detailOnServer = [DETAIL_A1, DETAIL_A2]

    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        const key = `${(cfg.method ?? 'get').toUpperCase()} ${cfg.url ?? ''}`
        if (key === `GET ${XGJ_RECEIPT_SUMMARY_URL}`) {
          return summaryOnServer
        }
        if (String(cfg.url) === '/api/business/order-source-groups/') {
          // ⭐ 「订单分组」下拉的字典。合成值。
          return {
            items: [{ id: 31, name: '合成分组甲' }],
            total: 1,
            page: 1,
            page_size: 200,
          }
        }
        if (key === `GET ${XGJ_RECEIPT_LIST_URL}`) {
          // 与后端同口径：`shop_identity` 非空 = 单店精确筛；空串 = 未识别桶。
          const identity = String(cfg.params?.shop_identity ?? '__absent__')
          const items = detailOnServer.filter(
            (row) => row.shop_identity === identity,
          )
          return {
            items,
            total: items.length,
            page: Number(cfg.params?.page ?? 1),
            page_size: Number(cfg.params?.page_size ?? 50),
          }
        }
        throw new Error(`测试没有为 ${key} 准备响应`)
      }),
    )
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('挂载：先挂样式、再塞标记、最后 init；缺省=前一天..前一天的汇总与合计上屏', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    // 陷阱 ③：运行期判据——bootstrap 时看不到标记，init 时看得到。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
    expect(root.innerHTML).toContain(PANEL_CLASS)
    // 缺省窗口 = 前一天（两个输入框都摆着同一个值，与后端缺省口径一致）。
    const yesterday = yesterdayStr()
    expect((root.querySelector(`#${DATE_FROM_ID}`) as HTMLInputElement).value).toBe(yesterday)
    expect((root.querySelector(`#${DATE_TO_ID}`) as HTMLInputElement).value).toBe(yesterday)
    // 汇总请求带的就是这两个缺省值。
    const cfg = callTo('get', XGJ_RECEIPT_SUMMARY_URL)
    expect(cfg.params).toEqual({ date_from: yesterday, date_to: yesterday })
    // 主表 + 合计行 + 口径声明（F4-D 的如实口径文本）。
    expect(tableRows(SUMMARY_TABLE_ID).map((r) => r.shop_identity)).toEqual(['EID-11001', 'EID-11002'])
    expect(root.querySelector(`#${TOTALS_ID}`)!.textContent).toContain('2 家店铺')
    expect(root.querySelector(`#${TOTALS_ID}`)!.textContent).toContain('100.00 元')
    expect(root.innerHTML).toContain(MESSAGES.caliberNote)
    expect(MESSAGES.caliberNote).toContain('买家实付减已退')
    expect(LAYUI.messages).toEqual([])
  })

  it('点店铺：真发带 shop_identity 的明细请求并把行摆上屏（金额缺失如实显示）', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    clickRow(SUMMARY_TABLE_ID, SHOP_A)
    await flush()

    const cfg = callTo('get', XGJ_RECEIPT_LIST_URL)
    expect(cfg.params).toMatchObject({ shop_identity: 'EID-11001', page: 1, page_size: 50 })
    expect(tableRows(DETAIL_TABLE_ID).length).toBe(2)
    // 明细区标题点名店铺。
    expect(root.querySelector(`#${DETAIL_TITLE_ID}`)!.textContent).toContain('合成店铺甲')
    // 金额缺失那一行**不是 0**：单元格渲染走 `amountCell` 的「缺失」分支。
    const renders = LAYUI.renders.find((r) => r.id === DETAIL_TABLE_ID)
    const cols = (renders!.cols as Array<Array<{ title?: string; templet?: unknown }>>)[0]!
    const amountCol = cols.find((c) => c.title === '实收金额（元）')
    expect(String((amountCol!.templet as (d: ReceiptDetailRow) => string)(DETAIL_A2))).toContain('缺失')
    expect(String((amountCol!.templet as (d: ReceiptDetailRow) => string)(DETAIL_A1))).toContain('45.60')
  })

  it('点未识别桶：空串参数照样发（后端按 identity 为空筛），标题兜「未识别店铺」', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    summaryOnServer = summaryPayload([SHOP_A, SHOP_UNKNOWN])
    detailOnServer = [DETAIL_A1, DETAIL_A2, DETAIL_UNKNOWN]
    clickRow(SUMMARY_TABLE_ID, SHOP_UNKNOWN)
    await flush()

    // ⚠️ 空串参数**必须出现在请求里**（fetchReceiptList 的判据是 `!== undefined`）：
    //    不发 = 后端按「不筛」处理 = 整窗明细都算这一家的，数字对不上（F2-D）。
    const cfg = callTo('get', XGJ_RECEIPT_LIST_URL)
    expect(cfg.params).toMatchObject({ shop_identity: '', page: 1, page_size: 50 })
    expect(tableRows(DETAIL_TABLE_ID).map((r) => r.order_no)).toEqual([DETAIL_UNKNOWN.order_no])
    expect(root.querySelector(`#${DETAIL_TITLE_ID}`)!.textContent).toContain(UNKNOWN_SHOP_LABEL)
  })

  it('改日期：真发新区间并重置明细区（旧店铺的选择不带走）', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    clickRow(SUMMARY_TABLE_ID, SHOP_A)
    await flush()
    expect(callCount('get', XGJ_RECEIPT_LIST_URL)).toBe(1)

    summaryOnServer = summaryPayload([SHOP_B])
    ;(root.querySelector(`#${DATE_FROM_ID}`) as HTMLInputElement).value = '2026-10-01'
    ;(root.querySelector(`#${DATE_TO_ID}`) as HTMLInputElement).value = '2026-10-03'
    ;(root.querySelector(`#${DATE_FROM_ID}`) as HTMLElement).dispatchEvent(new Event('change'))
    await flush()

    // 明细请求次数**没有**增加（明细区被重置），汇总请求带新区间。
    expect(callCount('get', XGJ_RECEIPT_LIST_URL)).toBe(1)
    expect(callTo('get', XGJ_RECEIPT_SUMMARY_URL).params).toEqual({
      date_from: '2026-10-01',
      date_to: '2026-10-03',
    })
    expect(tableRows(SUMMARY_TABLE_ID).map((r) => r.shop_identity)).toEqual(['EID-11002'])
    expect(root.querySelector(`#${DETAIL_TITLE_ID}`)!.textContent).toContain('点上面的一行店铺')
  })

  it('⭐ 订单分组：候选来自字典；改选即查（summary 带 source_group）；清空 ⇒ 键消失', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    const select = root.querySelector<HTMLSelectElement>(`#${SOURCE_GROUP_ID}`)!
    expect(select).not.toBeNull()
    expect(
      Array.from(select.querySelectorAll('option')).map((o) => [o.value, o.textContent]),
    ).toEqual([
      ['', '全部分组'],
      ['31', '合成分组甲'],
    ])
    // 缺省不筛：键不出现（⛔ 不发空串）。
    expect('source_group' in callTo('get', XGJ_RECEIPT_SUMMARY_URL).params!).toBe(false)

    select.value = '31'
    select.dispatchEvent(new Event('change'))
    await flush()
    expect(callTo('get', XGJ_RECEIPT_SUMMARY_URL).params).toMatchObject({ source_group: '31' })

    select.value = ''
    select.dispatchEvent(new Event('change'))
    await flush()
    expect('source_group' in callTo('get', XGJ_RECEIPT_SUMMARY_URL).params!).toBe(false)
  })

  it('⭐ 改日期后分组**不丢**（`applyRange` 重建 range 时显式带上 source_group）', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    const select = root.querySelector<HTMLSelectElement>(`#${SOURCE_GROUP_ID}`)!
    select.value = '31'
    select.dispatchEvent(new Event('change'))
    await flush()

    ;(root.querySelector(`#${DATE_FROM_ID}`) as HTMLInputElement).value = '2026-10-01'
    ;(root.querySelector(`#${DATE_TO_ID}`) as HTMLInputElement).value = '2026-10-03'
    ;(root.querySelector(`#${DATE_FROM_ID}`) as HTMLElement).dispatchEvent(new Event('change'))
    await flush()

    expect(callTo('get', XGJ_RECEIPT_SUMMARY_URL).params).toEqual({
      date_from: '2026-10-01',
      date_to: '2026-10-03',
      source_group: '31',
    })
  })

  it('⭐ 口径声明写明分组语义（店铺当前所属来源；历史行会变、未识别店铺不计入）', async () => {
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    expect(MESSAGES.caliberNote).toContain('店铺当前所属来源')
    expect(MESSAGES.caliberNote).toContain('未识别店铺')
    expect(root.innerHTML).toContain('店铺当前所属来源')
  })

  it('后端失败：如实显示后端 msg（`BizError`），不编第二句', async () => {
    mockedRequest.mockImplementation((cfg: Cfg) =>
      Promise.resolve().then(() => {
        if (String(cfg.url) === XGJ_RECEIPT_SUMMARY_URL) {
          throw new BizError(1000, 'date_from 不能晚于 date_to')
        }
        throw new Error('不应该发别的请求')
      }),
    )
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()
    expect(LAYUI.messages).toContain('date_from 不能晚于 date_to')
  })

  it('无节点：一句提示、不注入标记、不发任何业务请求', async () => {
    mockedFetchMe.mockResolvedValue(userWithoutNode())
    await page.mount(root, { onNavigate: vi.fn() })
    await flush()

    expect(LAYUI.messages).toContain(NO_ACCESS_MESSAGE)
    expect(root.innerHTML).toBe('')
    expect(LAYUI.renders.length).toBe(0)
    // 只有 fetchMe 那一次身份请求（它就是门本身），业务请求一个都没有。
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('纯函数：昨天串、合计行标记、汇总行透传与未识别桶兜底', () => {
    // 固定时钟：2026-10-22 的前一天是 2026-10-21（本地日，不碰 UTC）。
    expect(yesterdayStr(new Date(2026, 9, 22, 8, 0, 0))).toBe('2026-10-21')
    expect(yesterdayStr(new Date(2027, 0, 1, 0, 5, 0))).toBe('2026-12-31')

    const totals = totalsHtml({ shop_count: 2, count: 4, amount: '100.00', missing_count: 1 })
    expect(totals).toContain('id="xgjReceiptTotals"')
    expect(totals).toContain('100.00 元')
    // ⛔ 陷阱 ①：类名与选择器是两个常量，标记里不许出现 `class=".xxx"` 这种选择器形态。
    expect(totals).not.toContain('class=".')

    const rows = summaryTableData([SHOP_A])
    expect(rows[0]).toEqual({ ...SHOP_A })
    // 未识别桶的兜底链（F4-D）：后端名字空 + 组键空 ⇒「未识别店铺」；组键非空 ⇒ 组键原文。
    expect(shopDisplayName({ shop_identity: '', shop_name: '' })).toBe(UNKNOWN_SHOP_LABEL)
    expect(shopDisplayName({ shop_identity: 'EID-x', shop_name: '' })).toBe('EID-x')
    expect(shopDisplayName({ shop_identity: 'EID-x', shop_name: '有名字' })).toBe('有名字')
    expect(summaryTableData([SHOP_UNKNOWN])[0]).toEqual({
      ...SHOP_UNKNOWN,
      shop_name: UNKNOWN_SHOP_LABEL,
    })
  })
})
