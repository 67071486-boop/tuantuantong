import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: { get: vi.fn() } }))

import { AxiosError } from 'axios'
import { http, request } from '@/api/http'
import { BizError, HttpError } from '@/api/errors'
import {
  adjustPerformance,
  deletePerformances,
  downloadPerformanceExport,
  EXPORT_FALLBACK_FILENAME,
  exportFilename,
  fetchAlipayDuplicates,
  fetchPerformances,
  isJsonBlob,
  PERFORMANCE_ALIPAY_DUPLICATES_URL,
  PERFORMANCE_DELETE_URL,
  PERFORMANCE_EXPORT_URL,
  PERFORMANCES_URL,
  reviewPerformance,
  toPerformanceParams,
  type PerformanceListItem,
} from '../performances'

const mockedRequest = vi.mocked(request)
const mockedGet = vi.mocked(http.get)
const NO = '12345678901234567890'

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({} as never)
  mockedGet.mockReset()
})

describe('toPerformanceParams：空值不发送，0 与 false 必须留下', () => {
  it('空串 / undefined / null 一律去掉（后端把空串当「没传」，但这里连空串都不发）', () => {
    expect(
      toPerformanceParams({
        emp_type: '',
        review_status: undefined,
        settle_status: null,
        date_from: '   ',
      }),
    ).toEqual({})
  })

  it('保留真实值（含日期两端）', () => {
    expect(
      toPerformanceParams({
        emp_type: '打单工资',
        review_status: '待审核',
        settle_status: '未结账',
        date_from: '2026-06-20',
        date_to: '2026-09-20',
        page: 2,
        page_size: 50,
      }),
    ).toEqual({
      emp_type: '打单工资',
      review_status: '待审核',
      settle_status: '未结账',
      date_from: '2026-06-20',
      date_to: '2026-09-20',
      page: 2,
      page_size: 50,
    })
  })
})

describe('绩效三个端点的方法与路径（断言打在**真实出口**：request 的入参）', () => {
  it('列表走 GET /api/business/performances/（带筛选参数）', async () => {
    await fetchPerformances({ emp_type: '打单工资', review_status: '待审核', page: 1 })
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/business/performances/',
      params: { emp_type: '打单工资', review_status: '待审核', page: 1 },
    })
  })

  it('改金额走 POST /api/business/performances/{id}/adjust/（字段名逐条对齐后端序列化器）', async () => {
    await adjustPerformance(17, { taker_price: '88.00', reason: '绩效管理页行内改金额' })
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/performances/17/adjust/',
      data: { taker_price: '88.00', reason: '绩效管理页行内改金额' },
    })
  })

  it('审核走 POST /api/business/performances/{id}/review/（approved 必填）', async () => {
    await reviewPerformance(17, { approved: true, comment: 'ok' })
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/performances/17/review/',
      data: { approved: true, comment: 'ok' },
    })
  })

  it('审核驳回也是同一路径，载荷 approved=false（不是另造端点）', async () => {
    await reviewPerformance(3, { approved: false })
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/performances/3/review/',
      data: { approved: false },
    })
  })

  it('导出的列表路径就是后端注册的那一个（避免页面另抄一份字面量）', () => {
    expect(PERFORMANCES_URL).toBe('/api/business/performances/')
  })
})

/* ───────────── P4FU-B / C：删除、导出、支付宝查重（本轮新增的三条出口） ───────────── */

describe('删除记录：POST /api/business/performances/delete', () => {
  it('方法 / 路径 / body 逐项（键名对齐 `PerformanceIdsSerializer`）', async () => {
    await deletePerformances([11, 12])
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'post',
      url: '/api/business/performances/delete',
      data: { performance_ids: [11, 12] },
    })
  })

  it('路径**没有尾斜杠**（多一个会命中 router 的 detail 路由 `pk="delete"`）', () => {
    expect(PERFORMANCE_DELETE_URL).toBe('/api/business/performances/delete')
    expect(PERFORMANCE_DELETE_URL.endsWith('/')).toBe(false)
  })

  it('id 列表**原样**发出：不排序、不去重', async () => {
    await deletePerformances([12, 11, 12])
    const body = (mockedRequest.mock.calls[0]![0] as { data: { performance_ids: unknown } }).data
    expect(body.performance_ids).toEqual([12, 11, 12])
  })

  it('成功把后端的 `data` 原样交给调用方（`{deleted:[…]}`，**列表不是计数**）', async () => {
    mockedRequest.mockResolvedValueOnce({ deleted: [11, 12] } as never)
    await expect(deletePerformances([11, 12])).resolves.toEqual({ deleted: [11, 12] })
  })
})

describe('检测重复收款支付宝：GET /api/business/performances/alipay-duplicates', () => {
  it('无参 GET，路径没有尾斜杠', async () => {
    await fetchAlipayDuplicates()
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]![0]).toEqual({
      method: 'get',
      url: '/api/business/performances/alipay-duplicates',
    })
    expect(PERFORMANCE_ALIPAY_DUPLICATES_URL).toBe(
      '/api/business/performances/alipay-duplicates',
    )
  })

  it('出参形状原样透传（`{duplicates:[{alipay_account,count,users}]}`）', async () => {
    const payload = {
      duplicates: [
        {
          alipay_account: 'a@b.com',
          count: 2,
          users: [{ username: 'u1', job: '客服', is_active: true }],
        },
      ],
    }
    mockedRequest.mockResolvedValueOnce(payload as never)
    await expect(fetchAlipayDuplicates()).resolves.toEqual(payload)
  })
})

describe('导出记录：`downloadPerformanceExport`（**唯一**一个成功不是信封的端点）', () => {
 /** 一段 xlsx 的 Blob（后端成功时的形状）。
  * 导出是 `.xlsx`。按 CSV 的 MIME 或文件名断言会红。 */
  const xlsxBlob = (): Blob =>
    new Blob(['PK\u0003\u0004 fake-xlsx'], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
 /** 信封的 Blob（后端**失败**时的形状：HTTP 200 + `application/json`）。 */
  const envelopeBlob = (code: number, msg: string): Blob =>
    new Blob([JSON.stringify({ code, msg, data: null })], { type: 'application/json' })

  it('成功：GET + `responseType=blob` + 与列表**同一份**折参（含 0 值）', async () => {
 // 与后端 `PerformanceExportView` 的响应头逐字同形：**先 ASCII 兜底名、再 RFC 5987 中文名**。
    const chineseName = '效绩提成记录0923055917.xlsx'
    mockedGet.mockResolvedValueOnce({
      data: xlsxBlob(),
      headers: {
        'content-disposition':
          'attachment; filename="performances-20260922.xlsx"; ' +
          `filename*=UTF-8''${encodeURIComponent(chineseName)}`,
      },
    } as never)
    const result = await downloadPerformanceExport({
      emp_type: '打单工资',
      customer_type: 0,
      date_from: '2026-06-20',
      page: 3,
    })
    expect(mockedGet).toHaveBeenCalledTimes(1)
    expect(mockedGet.mock.calls[0]![0]).toBe(PERFORMANCE_EXPORT_URL)
    expect(mockedGet.mock.calls[0]![1]).toMatchObject({
      responseType: 'blob',
      params: {
        emp_type: '打单工资',
        customer_type: 0,
        date_from: '2026-06-20',
        page: 3,
      },
    })
 // 中文名只有 `filename*` 能承载 ⇒ 认它，⛔ 不认前面那个 ASCII 兜底名。
    expect(result.filename).toBe(chineseName)
    expect(result.blob).toBeInstanceOf(Blob)
  })

  it('`exportFilename`：先认 `filename*`（RFC 5987 解码），再认 `filename=`，都没有回兜底名', () => {
 // ① 两个都给时 **`filename*` 优先**（后端就是两个都给）
    expect(
      exportFilename(
        'attachment; filename="performances-20260922.xlsx"; ' +
          `filename*=UTF-8''${encodeURIComponent('效绩提成记录0923055917.xlsx')}`,
      ),
    ).toBe('效绩提成记录0923055917.xlsx')
 // ② 只有 ASCII `filename=` 时照旧能取（带引号 / 裸名）
    expect(exportFilename('attachment; filename="x-1.xlsx"')).toBe('x-1.xlsx')
    expect(exportFilename('attachment; filename=y.xlsx')).toBe('y.xlsx')
 // ③ 取不到 ⇒ 兜底名（**xlsx**，⛔ 不再是 `performances.csv`）
    expect(exportFilename(undefined)).toBe(EXPORT_FALLBACK_FILENAME)
    expect(exportFilename('attachment')).toBe(EXPORT_FALLBACK_FILENAME)
    expect(EXPORT_FALLBACK_FILENAME).toBe('performances.xlsx')
  })

  it('`filename*` 的百分号转义畸形（`decodeURIComponent` 抛）⇒ 回落 `filename=`，⛔ 不把异常抛给页面', () => {
    expect(
      exportFilename(
        "attachment; filename=\"fallback.xlsx\"; filename*=UTF-8''%E6%95%8",
      ),
    ).toBe('fallback.xlsx')
  })

  it('⭐ HTTP 200 + 信封（超上限 / 参数非法）⇒ 抛 `BizError`（带后端 msg），**不是**当成文件下载', async () => {
    const msg = '本次导出 12000 行，超过上限 10000 行，请先收窄筛选条件'
    mockedGet.mockResolvedValueOnce({ data: envelopeBlob(1000, msg), headers: {} } as never)
 // ⚠️ 只跑**一次**（`mockResolvedValueOnce` 只管一发；再跑一次会拿到 reset 后的 undefined）
    const err = await downloadPerformanceExport({}).then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(BizError)
    expect((err as BizError).code).toBe(1000)
    expect((err as Error).message).toBe(msg)
  })

  it('HTTP 403 ⇒ `HttpError`，msg 取自信封（与 `request()` 同一条 `mapHttpStatus` 判据）', async () => {
    const response = {
      status: 403,
      data: envelopeBlob(403, '你没有导出绩效记录的权限'),
      headers: {},
      statusText: 'Forbidden',
      config: {},
    }
    mockedGet.mockRejectedValueOnce(
      new AxiosError('Request failed', 'ERR_BAD_REQUEST', undefined, undefined, response as never),
    )
    const err = await downloadPerformanceExport({}).then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(HttpError)
    expect((err as Error).message).toBe('你没有导出绩效记录的权限')
  })

  it('`isJsonBlob`：按 Content-Type 判「信封 vs xlsx」（这就是两种形状的分流依据）', () => {
    expect(isJsonBlob(new Blob(['{}'], { type: 'application/json' }))).toBe(true)
    expect(isJsonBlob(new Blob(['{}'], { type: 'application/json; charset=utf-8' }))).toBe(true)
    expect(isJsonBlob(xlsxBlob())).toBe(false)
    expect(isJsonBlob(new Blob(['x']))).toBe(false)
  })
})

/** ⭐ **T5：绩效改金额的两个新键 + 绩效行的三个只读键**（spec §5.3 / §5.5，T4 契约）。
 * ⚠️ 前两条打在**运行期出口**（`request` 的入参）；第三条是**类型判据**——它由 `vue-tsc` 承担
 * （`PerformanceListItem` 少键 / 多键都在类型检查里红），运行期只做一次读取防「整段被摇掉」。 */
describe('绩效改金额 / 绩效行的三个新键（T4 契约）', () => {
  it('adjustPerformance 把 temp_amount / bonus_amount 原样送出去', async () => {
    await adjustPerformance(31, {
      temp_amount: '0.00',
      bonus_amount: '0.00',
      reason: '清空该接手绩效金额（订单列表-接手绩效弹层）',
    })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/performances/31/adjust/',
      data: {
        temp_amount: '0.00',
        bonus_amount: '0.00',
        reason: '清空该接手绩效金额（订单列表-接手绩效弹层）',
      },
    })
  })

  it('fetchPerformances 的 order_no + emp_type 两个参数原样出去', async () => {
    await fetchPerformances({ order_no: NO, emp_type: '打单工资' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: PERFORMANCES_URL,
      params: { order_no: NO, emp_type: '打单工资' },
    })
  })

  it('PerformanceListItem 的三个新键在类型上可用', () => {
 // 值的来源是**本页的真实出参**（`PerformanceListSerializer`）；⛔ 不许 `as PerformanceListItem`
 // 绕过：少一个必填键、多一个未声明键，都会在 `vue-tsc` 下红。
    const row: PerformanceListItem = {
      id: 1,
      order_no: NO,
 // ⭐ 来源单号是本页的真实出参（`PerformanceListSerializer.source_order_no`）。
 // ⚠️ 合成值、且**故意不是 19 位数字**（真值形状会命中 `pii-guard.spec.ts` 的
 // 「APPID 形状（19 位数字串）」内建模式，扫描面含 `src/frontend/src/**`）；
 // 与 `order_no` 不同形是**承重**的（同形则「正文取哪个字段」类判据恒真）。
      source_order_no: 'TB-合成-绩效-来源甲',
      user: 3,
      username: 'taker',
      emp_type: '打单工资',
      temp_amount: '10.00',
      bonus_amount: '0.00',
      deduct_mode: '',
      deduct_value: '0.00',
      taker_price: null,
      review_status: null,
      settle_status: '未结账',
      created_at: '2026-09-23T10:30:00+08:00',
      order_status: 'IN_PROGRESS',
      order_price: '100.00',
      net_receipts: null,
      refund_fee: '0.00',
      game_role: '',
      dl_project: '',
      wangwang: '',
      kefu: '',
      log_user_name: '',
      deduct_fee: '0.00',
      settled_at: null,
      settled_by: '',
      settled_amount: null,
      display_name: '示例打手',
      hours: 24,
      taker_project: '示例项目',
      negative_debt: false,
    }
    expect(row.display_name).toBe('示例打手')
    expect(row.hours).toBe(24)
    expect(row.taker_project).toBe('示例项目')
  })
})
