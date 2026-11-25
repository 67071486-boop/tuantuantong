import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  fetchSettlementPeople,
  fetchSettlementPreview,
  revokeSettlements,
  SETTLEMENT_PEOPLE_URL,
  SETTLEMENTS_PREVIEW_URL,
  SETTLE_REVOKE_URL,
  SETTLEMENTS_URL,
  SETTLE_URL,
  settlePerformances,
} from '../settlements'

const mockedRequest = vi.mocked(request)

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({} as never)
})

/** 结账的**传输层**契约（P4-FE-2）。页面侧的断言打在 `settlePerformances(ids, reason)` 上，
 * 这里把「那个调用真的会变成哪个 URL、哪个 body」钉死——两边合起来才覆盖 brief §1 的
 * 「断言请求 URL 与 body 逐项」。 */
describe('settlePerformances：POST /api/business/settlements/settle', () => {
  it('方法 / 路径 / body 逐项（键名对齐 `SettleRequestSerializer`）', async () => {
    await settlePerformances([11, 12], '')
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/settlements/settle',
      data: { performance_ids: [11, 12], reason: '' },
    })
  })

  it('`reason` 默认就是空串（页面不编理由，留痕侧由后端写 actor）', async () => {
    await settlePerformances([7])
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/settlements/settle',
      data: { performance_ids: [7], reason: '' },
    })
  })

  it('id 列表**原样**发出去：不排序、不去重、不转成字符串', async () => {
    await settlePerformances([12, 11, 12])
    const body = (mockedRequest.mock.calls[0]![0] as { data: { performance_ids: unknown } }).data
    expect(body.performance_ids).toEqual([12, 11, 12])
  })

  it('成功时把后端 `data` 原样交给调用方——两个字段是 **id 的列表**，不是条数', async () => {
    // ⚠️ 形状照**实测**（后端探针）：`SettleView.post` 把 `SettleResult` 的两个 list 原样塞进 `data`，
    //    没有 `len()`。上一版这里用 `2 / 1` 是错的形状，会让页面把 id 当条数念出来。
    mockedRequest.mockResolvedValueOnce({
      settled_performances: [11, 12],
      settled_adjustments: [7],
    } as never)
    await expect(settlePerformances([11, 12])).resolves.toEqual({
      settled_performances: [11, 12],
      settled_adjustments: [7],
    })
  })

  it('两个路径常量都是后端注册的那两个（**没有尾斜杠**）', () => {
    // 出处：`apps/business/urls.py` 的 `path("settlements", …)` 与 `path("settlements/settle", …)`。
    expect(SETTLEMENTS_URL).toBe('/api/business/settlements')
    expect(SETTLE_URL).toBe('/api/business/settlements/settle')
  })
})

/** 「结账对象人员」候选端点的**传输层**契约（D-0089）。页面侧（`emp-billsummary.spec.ts`）的断言打在「驱动真控件之后那次取数请求的入参」上，这里把「那个调用真的会变成哪个 URL、哪些参数」钉死。⚠️ **出参是 `data.items`（本端点不分页）**，不是 `Page<T>`——所以这里不回 `{items, total, page}`那种分页形状（套 `unwrapPage` 会拿不到 `items`）。 */
describe('fetchSettlementPeople：GET /api/business/settlements/people', () => {
  it('不带 game 时不送该参数；带 game 时送 game（正整数原样）', async () => {
    mockedRequest.mockResolvedValue({ items: [] } as never)
    await fetchSettlementPeople()
    const first = mockedRequest.mock.calls[0]![0] as {
      method: string
      url: string
      params: Record<string, unknown>
    }
    expect(first.method).toBe('get')
    expect(first.url).toBe('/api/business/settlements/people')
    expect(first.params).not.toHaveProperty('game')

    await fetchSettlementPeople(2)
    const second = mockedRequest.mock.calls[1]![0] as { params: Record<string, unknown> }
    expect(second.params).toMatchObject({ game: 2 })
  })

  it('`game` 传 0 / 负数时不送该参数（后端 `min_value=1` 会拒成 `code 1000`）', async () => {
    mockedRequest.mockResolvedValue({ items: [] } as never)
    await fetchSettlementPeople(0)
    await fetchSettlementPeople(-3)
    for (const call of mockedRequest.mock.calls) {
      // 判据是「参数表**恰好是空的**」——不是「没有 game 这个键」那么宽。
      expect((call[0] as { params: Record<string, unknown> }).params).toEqual({})
    }
  })

  it('返回 `data.items`（不是整页信封）', async () => {
    mockedRequest.mockResolvedValueOnce({ items: [{ id: 1, username: 'u1' }] } as never)
    await expect(fetchSettlementPeople()).resolves.toEqual([{ id: 1, username: 'u1' }])
  })

  it('路径常量**没有尾斜杠**（与 `/settlements` 同形；多一个会吃 301/404）', () => {
    expect(SETTLEMENT_PEOPLE_URL).toBe('/api/business/settlements/people')
    expect(SETTLEMENT_PEOPLE_URL.endsWith('/')).toBe(false)
  })
})

/** 撤销结账的**传输层**契约（P4FU-B / D-0072 ②）。页面侧的断言打在
 * `revokeSettlements(ids, reason)` 上，这里把「那个调用真的会变成哪个 URL、哪个 body」钉死。 */
describe('revokeSettlements：POST /api/business/settlements/revoke', () => {
  it('方法 / 路径 / body 逐项（`performance_ids` + **必填的 reason**）', async () => {
    await revokeSettlements([11, 12], '财务核对后撤销')
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/settlements/revoke',
      data: { performance_ids: [11, 12], reason: '财务核对后撤销' },
    })
  })

  it('路径**没有尾斜杠**（与 `/settle` 同形；多一个会吃 301/404 并静默丢 body）', () => {
    expect(SETTLE_REVOKE_URL).toBe('/api/business/settlements/revoke')
    expect(SETTLE_REVOKE_URL.endsWith('/')).toBe(false)
  })

  it('成功的两个字段是 **id 的列表**（不是计数），原样交给调用方', async () => {
    mockedRequest.mockResolvedValueOnce({
      revoked_performances: [11, 12],
      revoked_adjustments: [7],
    } as never)
    await expect(revokeSettlements([11, 12], '原因')).resolves.toEqual({
      revoked_performances: [11, 12],
      revoked_adjustments: [7],
    })
  })

  it('`reason` **原样**发出去（不 trim、不截断——必填/非空的判据在后端与页面弹层）', async () => {
    await revokeSettlements([7], '  两边有空格  ')
    const body = (mockedRequest.mock.calls[0]![0] as { data: { reason: string } }).data
    expect(body.reason).toBe('  两边有空格  ')
  })
})

/** 2026-10-03（清算绩效弹窗线 Task 3）：两个写端点的出参各多一个 `skipped_performances`
 * （后端 Task 2 / 计划 §3 T2.2 的契约），子页的成功提示取它的 `.length` 如实报数（Q3=A）。
 * 这里只钉**传透**（前端不做任何改名/计数），字段语义见 `../settlements.ts` 的类型注释。 */
describe('settle / revoke 的 `skipped_performances` 原样传透（id 列表，不是计数）', () => {
  it('settle：`skipped_performances` 原样交给调用方', async () => {
    mockedRequest.mockResolvedValueOnce({
      settled_performances: [11],
      settled_adjustments: [],
      skipped_performances: [12, 13],
    } as never)
    await expect(settlePerformances([11, 12, 13])).resolves.toEqual({
      settled_performances: [11],
      settled_adjustments: [],
      skipped_performances: [12, 13],
    })
  })

  it('revoke：`skipped_performances` 原样交给调用方', async () => {
    mockedRequest.mockResolvedValue({
      revoked_performances: [11],
      revoked_adjustments: [],
      skipped_performances: [12],
    } as never)
    await expect(revokeSettlements([11, 12], '原因')).resolves.toEqual({
      revoked_performances: [11],
      revoked_adjustments: [],
      skipped_performances: [12],
    })
  })
})

/** 结算预览端点的**传输层**契约（计划 §3 T2.1）。
 * ⚠️ 参数形态是**逗号串**（`performance_ids=1,2,3`），与两个**写**端点的 JSON 数组 body
 * **不同形**——别在两个方向之间共用参数模板（E-0007 同族）。 */
describe('fetchSettlementPreview：GET /api/business/settlements/preview', () => {
  it('方法 / 路径 / 参数逐项（`performance_ids` 是逗号串）', async () => {
    await fetchSettlementPreview([1, 2, 3])
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/business/settlements/preview',
      params: { performance_ids: '1,2,3' },
    })
  })

  it('id 列表**原样**拼串：不排序、不去重、不丢空位', async () => {
    await fetchSettlementPreview([12, 11, 12])
    const params = (mockedRequest.mock.calls[0]![0] as { params: Record<string, unknown> }).params
    expect(params.performance_ids).toBe('12,11,12')
    await fetchSettlementPreview([])
    const empty = (mockedRequest.mock.calls[1]![0] as { params: Record<string, unknown> }).params
    expect(empty.performance_ids).toBe('')
  })

  it('成功时把后端 `data` 原样交给调用方（含 `count` / 两个总额 / `owners` / `qr_codes`）', async () => {
    const payload = {
      count: 2,
      temp_total: '20.00',
      bonus_total: '0.00',
      settled_count: 1,
      unsettled_count: 1,
      owners: [{ user_id: 3, username: '测试昵称甲', amount: '0.00' }],
      qr_codes: [{ user_id: 3, username: '测试昵称甲', name: '', url: '/media/q.png?sig=x' }],
    }
    mockedRequest.mockResolvedValueOnce(payload as never)
    await expect(fetchSettlementPreview([1, 2])).resolves.toEqual(payload)
  })

  it('端点常量**没有尾斜杠**（与 `/settlements/settle` 同形；多一个会吃 301/404）', () => {
    expect(SETTLEMENTS_PREVIEW_URL).toBe('/api/business/settlements/preview')
    expect(SETTLEMENTS_PREVIEW_URL.endsWith('/')).toBe(false)
  })
})

/* ═══════════ D-0323：order_source 两态折参 + 来源分解端点═══════════ */

describe('toSettlementParams：order_source 两态（D-0323）', () => {
  it("数字 id 原样发；'none' 字面量原样发（⛔ 不被当成空值丢掉——那是「未指定来源」档）", async () => {
    const { fetchSettlements } = await import('../settlements')
    await fetchSettlements({ order_source: 5 })
    expect((mockedRequest.mock.calls[0]![0] as { params: Record<string, unknown> }).params).toMatchObject({ order_source: 5 })
    await fetchSettlements({ order_source: 'none' })
    expect((mockedRequest.mock.calls[1]![0] as { params: Record<string, unknown> }).params).toMatchObject({ order_source: 'none' })
  })

  it('null / 不传 ⇒ 不带该键（「没筛」，与「未指定来源」是两回事）', async () => {
    const { fetchSettlements } = await import('../settlements')
    await fetchSettlements({ order_source: null })
    await fetchSettlements({})
    for (const call of mockedRequest.mock.calls) {
      expect((call[0] as { params: Record<string, unknown> }).params).not.toHaveProperty('order_source')
    }
  })
})

describe('fetchSettlementDailySources：GET /api/business/settlements/daily-summary/sources（D-0323）', () => {
  it('方法 / 路径 / 参数恰为 {date}（路径没有尾斜杠，与 daily-summary 同形）', async () => {
    const { fetchSettlementDailySources, SETTLEMENT_DAILY_SOURCES_URL } = await import('../settlements')
    mockedRequest.mockResolvedValueOnce({ items: [] } as never)
    await fetchSettlementDailySources('2026-10-29')
    const config = mockedRequest.mock.calls[0]![0] as { method: string; url: string; params: Record<string, unknown> }
    expect(config.method).toBe('get')
    expect(config.url).toBe('/api/business/settlements/daily-summary/sources')
    expect(config.url.endsWith('/')).toBe(false)
    expect(config.params).toEqual({ date: '2026-10-29' })
    expect(SETTLEMENT_DAILY_SOURCES_URL).toBe('/api/business/settlements/daily-summary/sources')
  })

  it('返回 `data.items`（不分页，不是 Page<T>）', async () => {
    const { fetchSettlementDailySources } = await import('../settlements')
    const payload = { items: [{ source_id: 5, source_name: '甲店铺', order_count: 2, total_amount: '15.00' }] }
    mockedRequest.mockResolvedValueOnce(payload as never)
    await expect(fetchSettlementDailySources('2026-10-29')).resolves.toEqual(payload)
  })
})
