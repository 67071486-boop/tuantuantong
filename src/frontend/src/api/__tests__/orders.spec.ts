import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: { get: vi.fn() } }))

import { AxiosError } from 'axios'
import { http, request } from '@/api/http'
import { BizError, HttpError } from '@/api/errors'
import {
  assignOrder,
  batchAssignOrders,
  batchChangeHallPrices,
  batchChangePaymentStatus,
  batchChangePrices,
  batchChangeTypes,
  batchDeleteOrders,
  batchDeleteV2Orders,
  batchOrderAction,
  batchRecomputeCommission,
  batchRefundOrders,
  batchTransitionOrders,
  changePrice,
  clearTaker,
  createOrder,
  downloadOrderExport,
  fetchAssigneeOptions,
  fetchBatchTransitionTargets,
  fetchOrder,
  fetchOrderAudit,
  fetchOrders,
  fetchPenaltyChildren,
  fetchSplitMembers,
  isOrderNoShape,
  ORDER_EXPORT_URL,
  parsePastedText,
  partialRefund,
  toLocalIso,
  toOrderParams,
  transitionOrder,
  type AuditAction,
  type BatchOutcome,
} from '../orders'

const mockedRequest = vi.mocked(request)
const mockedGet = vi.mocked(http.get)
const NO = '12345678901234567890'

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({})
})

describe('toOrderParams：空值不发送，布尔与 0 必须留下', () => {
  it('空串 / undefined / null 一律去掉', () => {
    expect(
      toOrderParams({ status: '', game: null, keyword: undefined, tag: '  ' }),
    ).toEqual({})
  })

  it('is_urgent:false 必须发出去（否则「不加急」永远筛不出来）', () => {
    expect(toOrderParams({ is_urgent: false })).toEqual({ is_urgent: false })
  })

  it('page:0 不会被当成空值丢掉', () => {
    expect(toOrderParams({ page: 0 })).toEqual({ page: 0 })
  })

  it('保留真实值', () => {
    expect(toOrderParams({ status: 'PAUSED', game: 3, page_size: 50 })).toEqual({
      status: 'PAUSED',
      game: 3,
      page_size: 50,
    })
  })
})

describe('toLocalIso：带时区偏移的 ISO 8601', () => {
  it('与本地时间一致（本机在 UTC 偏移为整小时的时区时逐字相等）', () => {
    const d = new Date(2026, 8, 20, 0, 0, 0)
    const offsetMin = -d.getTimezoneOffset()
    const sign = offsetMin < 0 ? '-' : '+'
    const abs = Math.abs(offsetMin)
    const pad = (n: number) => String(n).padStart(2, '0')
    const offset = `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
    expect(toLocalIso(d)).toBe(`2026-09-20T00:00:00${offset}`)
  })

  it('带时分秒且以 ±HH:MM 结尾——不带偏移就不是本函数的契约', () => {
    expect(toLocalIso(new Date(2026, 8, 20, 13, 45, 6))).toMatch(
      /^2026-09-20T13:45:06[+-]\d{2}:\d{2}$/,
    )
  })

  it('本地日不会被 toISOString 的 UTC 转换整体挪一天', () => {
    // 东八区的 2026-09-20T00:00 用 toISOString() 会变成 2026-09-19T16:00:00Z。
    // 本函数必须让「本地日期」那一段保持 20 日。
    expect(toLocalIso(new Date(2026, 8, 20, 0, 0, 0)).startsWith('2026-09-20T00:00:00')).toBe(true)
  })
})

describe('订单号形状校验：形状不对不发请求', () => {
  it('20 位纯数字合法', () => {
    expect(isOrderNoShape(NO)).toBe(true)
  })

  // ⚠️ 用例名别写「后端正则匹配不上会回 HTML 404」那种绝对句：只有 router 生成的
  // `orders/{pk}/…` 才是 404，退款/大厅那些显式 `path()` 用宽松转换器、会回 200 + 业务码
  // （见 `api/orders.ts` 的 `isOrderNoShape` 与 `partialRefund` docstring）。
  it('19 位、21 位、含字母都不合法', () => {
    expect(isOrderNoShape('1234567890123456789')).toBe(false)
    expect(isOrderNoShape('123456789012345678901')).toBe(false)
    expect(isOrderNoShape('1234567890123456789a')).toBe(false)
  })

  it('形状不对时抛前端错误且**一次请求都不发**', async () => {
    await expect(fetchOrder('abc')).rejects.toThrow('订单号格式不正确（应为 20 位数字）：abc')
    await expect(fetchOrderAudit('abc')).rejects.toThrow('订单号格式不正确（应为 20 位数字）：abc')
    await expect(changePrice('abc', { total_price: '1.00' })).rejects.toThrow(
      '订单号格式不正确（应为 20 位数字）：abc',
    )
    await expect(transitionOrder('abc', { target: 'REFUNDED' })).rejects.toThrow(
      '订单号格式不正确（应为 20 位数字）：abc',
    )
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})

describe('端点与载荷', () => {
  it('罚款子单展开沿用范围筛选并取齐各页，原单号搜索不进请求', async () => {
    const child = { order_no: '20261116000000003101' }
    const second = { order_no: '20261116000000003102' }
    mockedRequest
      .mockResolvedValueOnce({ items: [child], total: 2, page: 1, page_size: 1 })
      .mockResolvedValueOnce({ items: [second], total: 2, page: 2, page_size: 1 })

    const items = await fetchPenaltyChildren(NO, { keyword: NO, game: 3, status: 'COMPLETED' })

    expect(items).toEqual([child, second])
    expect(mockedRequest.mock.calls.map(([config]) => config.params)).toEqual([
      { penalty_parent_order_no: NO, game: 3, status: 'COMPLETED', page: 1, page_size: 300 },
      { penalty_parent_order_no: NO, game: 3, status: 'COMPLETED', page: 2, page_size: 300 },
    ])
  })

  it('拆单成员查询透传筛选条件并使用列表端点', async () => {
    mockedRequest.mockResolvedValueOnce({ items: [] })
    await fetchSplitMembers('group-1', { sales_user: 19, kefu_type: '0', status: 'ISSUE', game: 3 })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get', url: '/api/business/orders/',
      params: { sales_user: 19, kefu_type: '0', status: 'ISSUE', game: 3, split_id: 'group-1', page_size: 200 },
    })
  })
  it('列表走 GET /api/business/orders/ 且 params 经过空值过滤', async () => {
    await fetchOrders({ status: 'PAUSED', keyword: '', page: 2, page_size: 50 })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/orders/',
      params: { status: 'PAUSED', page: 2, page_size: 50 },
    })
  })

  it('fetchOrders() 不带任何参数时 params 是空对象（不是 undefined）', async () => {
    await fetchOrders()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/orders/',
      params: {},
    })
  })

  it('详情走 GET /api/business/orders/<单号>/', async () => {
    await fetchOrder(NO)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: `/api/business/orders/${NO}/`,
    })
  })

  it('指派候选名单走 GET /api/business/orders/assignee-options/，参数原样透传（D-0146 方案②）', async () => {
    await fetchAssigneeOptions({ role: '代练员', business_type: 1, key: '三角洲', limit: 200 })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/orders/assignee-options/',
      params: { role: '代练员', business_type: 1, key: '三角洲', limit: 200 },
    })
  })

  it('fetchAssigneeOptions() 不带参数时 params 是空对象（不是 undefined）', async () => {
    await fetchAssigneeOptions()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/orders/assignee-options/',
      params: {},
    })
  })

  it('fetchAssigneeOptions 原样交回 data（D-0147 新加的 roles 不被本层裁掉）', async () => {
    // 这一层的职责只是把信封解开；`roles` 是本批新增的第三个键（B12b 的渲染来源）。
    // 判据打成「原样」：哪天真在这里挑键（丢掉 roles），这条会红。
    const payload = { items: [], truncated: false, roles: ['测试职位甲'] }
    mockedRequest.mockResolvedValueOnce(payload as never)
    await expect(fetchAssigneeOptions()).resolves.toBe(payload)
  })

  it('录单走 POST /api/business/orders/，载荷原样透传（必填闸在录单页，不在本层）', async () => {
    await createOrder({ game: 1, project_text: '深渊满星' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/',
      data: { game: 1, project_text: '深渊满星' },
    })
  })

  it('改价走 /price/，载荷含 reason 且金额以字符串送（允许负数）', async () => {
    await changePrice(NO, { total_price: '-199.50', reason: '客户退款', actor: '客服小张' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/price/`,
      data: { total_price: '-199.50', reason: '客户退款', actor: '客服小张' },
    })
  })

  it('流转走 /transition/，target=REFUNDED 就是「取消」', async () => {
    await transitionOrder(NO, { target: 'REFUNDED', actor: '客服小张' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/transition/`,
      data: { target: 'REFUNDED', actor: '客服小张' },
    })
  })

  it('粘贴解析走 /parse/，载荷只有 content（不建单）', async () => {
    await parsePastedText('账号：acc001\n总价：-199.50')
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/parse/',
      data: { content: '账号：acc001\n总价：-199.50' },
    })
  })

  it('留痕走 /audit/，返回数组（倒序由后端保证）', async () => {
    // `action` 取值是**新表 code**（A9「一次改到位」）：`order.transition` / `order.change_taker_price` /
    // `performance.review` …，不再是旧表的两个 legacy 取值。
    //
    // 类型钉（**不加运行时断言**：`expect(x).toBe(它的字面值)` 是同义反复，钉住东西的是那个类型标注）：
    // `/audit/` 是 `OperationLog.objects.filter(order_no=…)`，**任何**已登记动作都可能挂在这一单上；
    // 写成两个成员的窄联合时，下面这一行会在 `pnpm typecheck` 下红（这就是本类型的 RED）。
    const anyRegistered: AuditAction = 'order.view_credentials'
    mockedRequest.mockResolvedValue([
      {
        action: 'order.change_taker_price',
        actor: '客服小张',
        reason: '客户退款',
        from: '199.50',
        to: '-199.50',
        created_at: '2026-09-20T10:00:00+08:00',
      },
      {
        action: anyRegistered,
        actor: '客服小张',
        reason: '',
        from: '',
        to: '',
        created_at: '2026-09-20T09:00:00+08:00',
      },
    ])
    await expect(fetchOrderAudit(NO)).resolves.toEqual([
      {
        action: 'order.change_taker_price',
        actor: '客服小张',
        reason: '客户退款',
        from: '199.50',
        to: '-199.50',
        created_at: '2026-09-20T10:00:00+08:00',
      },
      {
        action: 'order.view_credentials',
        actor: '客服小张',
        reason: '',
        from: '',
        to: '',
        created_at: '2026-09-20T09:00:00+08:00',
      },
    ])
    // 端点路径也必须被钉住：本模块的职责就是让「路径 + 参数」只有一处落点。
    // 只断言返回值的话，URL 打错（如 `/audit-typo/`）不会有任何测试变红（实测过）。
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: `/api/business/orders/${NO}/audit/`,
      params: { scope: 'business' },
    })
  })

  it('流转响应的 allowed_transitions 直接可用——**不要再发一次详情请求**', async () => {
    const order = {
      order_no: NO,
      status: 'REFUNDED',
      allowed_transitions: [] as string[],
    }
    mockedRequest.mockResolvedValue(order)
    await expect(transitionOrder(NO, { target: 'REFUNDED' })).resolves.toEqual(order)
    // 打桩只被调用了一次：如果实现里为了刷新白名单又发了 GET 详情，这里会是 2。
    expect(mockedRequest).toHaveBeenCalledTimes(1)
  })
})

/** ⭐ **P4-FE-3：部分退款的出口**（`POST /api/business/orders/<单号>/refunds`）。判据两路（缺一不可）：1. **路径与载荷逐项**——按模块职责，URL 只有这一处落点，打错（例如`…/refund/`、`…/refunds/` 多一个尾斜杠）不会有任何测试变红；2. **订单号形状先拦**——`assertOrderNo` 拦住后**一次请求都不发**。⚠️ **理由不是「不拦就是 HTML 404」**（那句实测为假，见 `api/orders.ts` 的`partialRefund` docstring）：本端点是**显式 `path()` + 宽松 `<order_no:…>` 转换器**，形状不对时后端回的是 **200 + 业务码 `1000`**，只有 router 生成的那条 `orders/{pk}/…`（`lookup_value_regex = r"\d{20}"`）才是 404 HTML。前端拦一道的作用是**省一次往返**。 */
describe('设置累计退款额：POST /api/business/orders/<单号>/refunds', () => {
  it('路径无尾斜杠、body 恰好 `{refund_total, reason, request_id}`（后端 `RefundRequestSerializer` 只收这三个）', async () => {
    await partialRefund(NO, { refund_total: '50.00', reason: '退货差价', request_id: 'r-1' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/refunds`,
      data: { refund_total: '50.00', reason: '退货差价', request_id: 'r-1' },
    })
  })

  it('累计额以**字符串**送（负数/0 形态合法由后端判，前端不换算、不加下界）', async () => {
    await partialRefund(NO, { refund_total: '-1.50', reason: 'x', request_id: 'r-2' })
    const cfg = mockedRequest.mock.calls[mockedRequest.mock.calls.length - 1]![0] as {
      data: { refund_total: string }
    }
    expect(cfg.data.refund_total).toBe('-1.50')
  })

  it('订单号形状不对 ⇒ 抛前端错误且**一次请求都不发**', async () => {
    await expect(
      partialRefund('abc', { refund_total: '1', reason: 'x', request_id: 'r' }),
    ).rejects.toThrow('订单号格式不正确（应为 20 位数字）：abc')
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('返回的是该条退款记录（后端 `RefundSerializer` 的形状）', async () => {
    const record = {
      id: 1,
      order_no: NO,
      kind: 'SET',
      amount: '50.00',
      reason: '退货差价',
      operator: '客服小张',
      request_id: 'r-3',
      created_at: '2026-09-21T10:00:00+08:00',
    }
    mockedRequest.mockResolvedValue(record)
    await expect(
      partialRefund(NO, { refund_total: '50.00', reason: '退货差价', request_id: 'r-3' }),
    ).resolves.toEqual(record)
  })
})

/** ⭐ **T5：`/assign/` 与 `/clear-taker/` 的载荷契约**（spec §5.1 / §5.2）。
 * 判据是值相等：多带一个有值的多余键会红。
 * `undefined` 的键会被忽略，这条不会红。出网前 `JSON.stringify` 也会丢掉这种键。 */
describe('指派 / 清除接单人的载荷（T3 契约）', () => {
  it('assignOrder 送 user_id 与可选键，URL 带尾斜杠（`fee` 仍被契约接受）', async () => {
    await assignOrder(NO, {
      user_id: 7,
      fee: '88.50',
      hours: 24,
      project: '三角洲',
    })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/assign/`,
      data: { user_id: 7, fee: '88.50', hours: 24, project: '三角洲' },
    })
  })

  it('assignOrder 送 `price`（D-0147：写 Order.hall_price，指派弹层的新出口）', async () => {
    await assignOrder(NO, { user_id: 7, price: '88.50', hours: 24, project: '三角洲' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/assign/`,
      data: { user_id: 7, price: '88.50', hours: 24, project: '三角洲' },
    })
  })

  it('assignOrder 只送 user_id 时不带任何多余键（C8 的脏标记出口）', async () => {
    await assignOrder(NO, { user_id: 7 })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/assign/`,
      data: { user_id: 7 },
    })
  })

  it('clearTaker 送 isDelEmp，缺省时不带 body', async () => {
    await clearTaker(NO, { isDelEmp: 'Y' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/clear-taker/`,
      data: { isDelEmp: 'Y' },
    })
    mockedRequest.mockClear()
    await clearTaker(NO)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/clear-taker/`,
    })
  })
})

/** ⭐ **批量出口（Phase B · Task 2，spec §4.4 / §D2/D3/D10）**。契约（**实测**，⛔ 别猜）：- 端点 = `POST /api/business/orders/batch/<动作>/`（9 条 `@action(url_path="batch/<动作>")`注册在 `OrderViewSet` 上，`src/apps/business/api/order_views.py:1935-2266`）；- body 恒含 `order_nos`（数组）与 `dry_run`（**必填布尔**：`false` 是合法取值，⛔ 不能与「没传」混同——E-0021）；- 响应 `data = {dry_run, eligible, ineligible, applied}`；改状态的**读取形态**（不传 `target`）另有 `targets` 与 `per_order`（`order_batch.py:66-88`）。⚠️ 夹具一律**合成值**（E-0077）：单号用 `'2'.repeat(20)` 这种形状串，⛔ 不抄真单号。 */
describe('批量出口：POST /api/business/orders/batch/<动作>/', () => {
  const NO_A = '2'.repeat(20)
  const NO_B = '3'.repeat(20)
  const NOS = [NO_A, NO_B]
  /** 合成畸形单号（19 位，形状闸拦的就是它）：⛔ 不抄真单号（E-0077）。 */
  const BAD_NO = '2'.repeat(19)

  it('batchOrderAction 把动作拼进 URL、并原样带 order_nos 与 dry_run', async () => {
    await batchOrderAction('price', { order_nos: NOS, dry_run: true, total_price: '88.00' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/batch/price/',
      data: { order_nos: NOS, dry_run: true, total_price: '88.00' },
    })
  })

  it('批量出口对每个单号过一遍形状闸（畸形单号不发请求）', async () => {
    await expect(
      batchOrderAction('price', { order_nos: [NO_A, 'not-an-order-no'], dry_run: true }),
    ).rejects.toThrow('订单号格式不正确（应为 20 位数字）：not-an-order-no')
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  /** ⭐ Task 2 评审 Important ① 的**运行时判据**：原实现读 `payload.order_nos ?? []`
   * ⇒ 缺键时循环跑 0 次、形状闸**静默跳过**、请求照发。类型收窄只挡得住 TS 调用方，
   * 这条钉住运行期也**响亮失败**（`TypeError`）且**一次请求都不发**。 */
  it('缺 `order_nos` 时响亮失败（⛔ 不静默跳过形状闸、也不发请求）', async () => {
    await expect(
      batchOrderAction('price', {} as { order_nos: string[] } & Record<string, unknown>),
    ).rejects.toThrow('batchOrderAction(price): payload.order_nos 必须是字符串数组')
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('`dry_run: false` 是合法取值，原样送出去（⛔ 不被当成「没传」丢掉）', async () => {
    await batchOrderAction('transition', { order_nos: [NO_A], dry_run: false, target: 'REFUNDED' })
    const cfg = mockedRequest.mock.calls[0]![0] as { data: { dry_run: boolean } }
    expect(cfg.data.dry_run).toBe(false)
    expect('dry_run' in cfg.data).toBe(true)
  })

  // 每个动作一条**独立**用例（`it.each`）⇒ 失败时报告里直接点名是哪个动作哪条 URL。
  it.each([
    {
      action: 'assign',
      run: () => batchAssignOrders({ order_nos: NOS, dry_run: true, user_id: 7, price: '66.00' }),
      data: { order_nos: NOS, dry_run: true, user_id: 7, price: '66.00' },
    },
    {
      action: 'transition',
      run: () => batchTransitionOrders({ order_nos: NOS, dry_run: false, target: 'REFUNDED' }),
      data: { order_nos: NOS, dry_run: false, target: 'REFUNDED' },
    },
    {
      action: 'types',
      run: () => batchChangeTypes({ order_nos: NOS, dry_run: true, order_type: 'COMPANION' }),
      data: { order_nos: NOS, dry_run: true, order_type: 'COMPANION' },
    },
    {
      action: 'price',
      run: () =>
        batchChangePrices({ order_nos: NOS, dry_run: true, total_price: '-1.50', reason: '改价' }),
      data: { order_nos: NOS, dry_run: true, total_price: '-1.50', reason: '改价' },
    },
    {
      action: 'hall-price',
      run: () => batchChangeHallPrices({ order_nos: NOS, dry_run: true, price: '50%' }),
      data: { order_nos: NOS, dry_run: true, price: '50%' },
    },
    {
      action: 'payment-status',
      run: () =>
        batchChangePaymentStatus({ order_nos: NOS, dry_run: false, payment_status: 'SETTLED' }),
      data: { order_nos: NOS, dry_run: false, payment_status: 'SETTLED' },
    },
    {
      action: 'delete',
      run: () => batchDeleteOrders({ order_nos: NOS, dry_run: false, isDelEmp: 'Y' }),
      data: { order_nos: NOS, dry_run: false, isDelEmp: 'Y' },
    },
    {
      action: 'delete-v2',
      run: () =>
        batchDeleteV2Orders({ order_nos: NOS, dry_run: false, isDelEmp: 'Y', reason: '测试原因' }),
      data: { order_nos: NOS, dry_run: false, isDelEmp: 'Y', reason: '测试原因' },
    },
    {
      action: 'recompute-commission',
      run: () => batchRecomputeCommission({ order_nos: NOS, dry_run: true }),
      data: { order_nos: NOS, dry_run: true },
    },
  ])('薄包装 $action 拼对动作段、body 原样透传', async (c) => {
    await c.run()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/batch/${c.action}/`,
      data: c.data,
    })
  })

  /** ⭐ 形状闸**不只长在通用函数上**：薄包装就是调用点，畸形单号必须**在发请求之前**失败。评审点名「闸的判据只打在通用函数与读取形态上」⇒ 这条驱动**真薄包装**，且断言到**具体错误文案**（只写 `toThrow()` 会让「因为别的原因抛」也算过）。⚠️ **`batchDeleteOrders` 已于 2026-09-27 从本列表移出——那是有意豁免（R-D1），⛔ 不是「漏了」、也不是「待补」**：业务方裁定批量**删除**要能把请求发出去（清理存量垃圾行；见文末「批量删除豁免 20 位形状闸」那一节的正面用例与反向边界）。本列表这两条薄包装仍守 20 位闸。 */
  it.each([
    {
      name: 'batchAssignOrders',
      run: () => batchAssignOrders({ order_nos: [BAD_NO], dry_run: true, user_id: 7 }),
    },
    {
      name: 'batchRefundOrders',
      run: () =>
        batchRefundOrders({
          order_nos: [BAD_NO],
          dry_run: true,
          refund_total: '50.00',
          reason: '退货差价',
          request_ids: ['r-1'],
        }),
    },
  ])('薄包装 $name 也过形状闸（畸形单号不发请求）', async (c) => {
    await expect(c.run()).rejects.toThrow(`订单号格式不正确（应为 20 位数字）：${BAD_NO}`)
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('退款的批量形态送 `request_ids`（逐笔一个），⛔ 不送单笔那个必填的 `request_id`', async () => {
    await batchRefundOrders({
      order_nos: NOS,
      dry_run: true,
      refund_total: '50.00',
      reason: '退货差价',
      request_ids: ['r-1', 'r-2'],
    })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/batch/refunds/',
      data: {
        order_nos: NOS,
        dry_run: true,
        refund_total: '50.00',
        reason: '退货差价',
        request_ids: ['r-1', 'r-2'],
      },
    })
    const cfg = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect('request_id' in cfg.data).toBe(false)
  })

  it('读取形态（不传 target）**仍带 `dry_run: true`**——后端字段必填，虽不参与判断', async () => {
    await fetchBatchTransitionTargets(NOS)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/batch/transition/',
      data: { order_nos: NOS, dry_run: true },
    })
    const cfg = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect('target' in cfg.data).toBe(false)
  })

  it('读取形态的响应原样交回：`targets` 与 `per_order` 不被本层裁掉', async () => {
    // ⚠️ 夹具按**后端真形状**写：读取形态 `eligible` / `applied` **恒空**
    //    （`order_batch.py:79/89` 写明「这条请求没提交任何目标」），取不到行的单号进 `ineligible`。
    const outcome: BatchOutcome = {
      dry_run: true,
      targets: ['IN_PROGRESS', 'COMPLETED'],
      per_order: [
        { order_no: NO_A, allowed: ['IN_PROGRESS'] },
        { order_no: NO_B, allowed: ['IN_PROGRESS', 'COMPLETED'] },
      ],
      eligible: [],
      ineligible: [{ order_no: '9'.repeat(20), code: 1002, msg: '记录不存在或已被删除' }],
      applied: [],
    }
    mockedRequest.mockResolvedValue(outcome)
    await expect(fetchBatchTransitionTargets(NOS)).resolves.toEqual(outcome)
  })

  it('读取形态也过形状闸（畸形单号不发请求）', async () => {
    await expect(fetchBatchTransitionTargets(['abc'])).rejects.toThrow(
      '订单号格式不正确（应为 20 位数字）：abc',
    )
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})

/** 订单导出：`GET /api/business/orders/export` 是**本仓第二个「成功不是信封」的端点**（第一个是绩效导出，形状照 `performances.spec.ts:174` 那一节）。判据打在**真 `api/orders.ts` 拼出来的请求配置**上（E-0009 同族）：`responseType: 'blob'` 是承重的——少了它 axios 会把 xlsx 字节当文本解，落盘的文件必坏，而这条在别处**看不出来**（**本文件枚举到的**成功路径只断言「不弹这两句提示」，不看 `responseType`）。 */
describe('订单导出：downloadOrderExport（成功不是信封的第二个端点）', () => {
  const xlsxBlob = (): Blob =>
    new Blob(['PK\u0003\u0004 fake-xlsx'], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
  const envelopeBlob = (code: number, msg: string): Blob =>
    new Blob([JSON.stringify({ code, msg, data: null })], { type: 'application/json' })

  beforeEach(() => {
    mockedGet.mockReset()
  })

  it('成功：GET + responseType=blob + 与列表同一份折参；文件名认 filename*', async () => {
    const chineseName = '订单导出0927102030.xlsx'
    mockedGet.mockResolvedValueOnce({
      data: xlsxBlob(),
      headers: {
        'content-disposition':
          'attachment; filename="orders-20260927.xlsx"; ' +
          `filename*=UTF-8''${encodeURIComponent(chineseName)}`,
      },
    } as never)
    const result = await downloadOrderExport({ status: 'UNASSIGNED', price_min: '0' })
    expect(mockedGet).toHaveBeenCalledTimes(1)
    // ⛔ 字面量（不拿被测常量自证，E-0024）：路径**不带**尾斜杠是与后端 `urls.py` 的接口事实。
    expect(ORDER_EXPORT_URL).toBe('/api/business/orders/export')
    expect(mockedGet.mock.calls[0]![0]).toBe('/api/business/orders/export')
    expect(mockedGet.mock.calls[0]![1]).toMatchObject({
      responseType: 'blob',
      params: { status: 'UNASSIGNED', price_min: '0' },
    })
    expect(result.filename).toBe(chineseName)
    expect(result.blob).toBeInstanceOf(Blob)
  })

  it('没有 Content-Disposition ⇒ 用**订单侧**兜底名 orders.xlsx（不是绩效那份）', async () => {
    // 这条同时钉两件事：① 跨模块复用的 `exportFilename` **必须**收到订单侧的兜底名；
    // ② `ORDER_EXPORT_FALLBACK_FILENAME` 的值本身（⛔ 后缀必须与真实字节一致）。
    mockedGet.mockResolvedValueOnce({ data: xlsxBlob(), headers: {} } as never)
    const result = await downloadOrderExport({})
    expect(result.filename).toBe('orders.xlsx')
  })

  it('HTTP 200 + 信封（超上限 / 参数非法）⇒ 抛带后端 msg 的 BizError，不是当文件下载', async () => {
    const msg = '本次导出 12000 行，超过上限 10000 行，请先收窄筛选条件'
    mockedGet.mockResolvedValueOnce({ data: envelopeBlob(1000, msg), headers: {} } as never)
    const err = await downloadOrderExport({}).then(() => null, (e: unknown) => e)
    expect(err).toBeInstanceOf(BizError)
    expect((err as BizError).code).toBe(1000)
    expect((err as Error).message).toBe(msg)
  })

  it('HTTP 403 ⇒ HttpError，msg 取自信封', async () => {
    const response = {
      status: 403,
      data: envelopeBlob(403, '你没有导出订单的权限'),
      headers: {},
      statusText: 'Forbidden',
      config: {},
    }
    mockedGet.mockRejectedValueOnce(
      new AxiosError('Request failed', 'ERR_BAD_REQUEST', undefined, undefined, response as never),
    )
    const err = await downloadOrderExport({}).then(() => null, (e: unknown) => e)
    expect(err).toBeInstanceOf(HttpError)
    expect((err as Error).message).toBe('你没有导出订单的权限')
  })

  it('勾选档只送 order_nos（页面拼的逗号串原样带过去）', async () => {
    mockedGet.mockResolvedValueOnce({ data: xlsxBlob(), headers: {} } as never)
    await downloadOrderExport({ order_nos: `${'2'.repeat(20)},${'3'.repeat(20)}` })
    expect(mockedGet.mock.calls[0]![1]).toMatchObject({
      params: { order_nos: `${'2'.repeat(20)},${'3'.repeat(20)}` },
    })
  })
})

/** ⭐ **R-D1（业务方裁定）：批量**删除**不再要求 20 位单号**。## 为什么整条批量路径都不需要那个形状（而单笔/详情需要）那条 `\d{20}` 是**前端对后端 URL 路由约束的复制**：`OrderViewSet.lookup_value_regex = r"\d{20}"`⇒ 畸形单号匹配不到 router 那条 `orders/{pk}/…`、只会得到 **404 HTML**，用户看不到可读原因。批量端点**不走 URL 路径**：单号在**请求体**里，后端自己的契约只是「非空字符串」（`apps/business/api/order_batch.py::parse_order_nos`），取不到的行进 per-item `ineligible`⇒ 后端**仍会拒那一笔**，但那**不是**整批 1000、也不是 404 HTML。## 那为什么**只**放开 `delete`（⛔ 别把这节读成「漏了别的动作」）业务方裁定：只有**清理存量垃圾行**（本机演示库里那条非 20 位的合成行就是这类）
 * 必须把请求**发出去**——前端拦一道的本意是「省一次往返」，而删除这一条路的**目的**正是发请求⇒ 取舍在这里翻转（原口径 spec §4.4 的更正块、`D-0160` 的追加条目都记了这条**有意的不对称**）。其余批量动作与「打开详情 / 截图」的单笔路径**一字不动**：那边拦下去是有意义的（详情真的会落进 404 HTML）。第 ② 条反向用例就是钉这条边界的。⚠️ 夹具一律**合成值**（E-0077）：这里用 16 位形状串复现「**非空但非 20 位**」这一档，⛔ 不抄真单号。 */
describe('批量删除豁免 20 位形状闸（R-D1：只放开 delete）', () => {
  /** 合成 **16 位**单号（**形状**照本机演示库那条删不掉的垃圾行；⛔ 不是真单号，E-0077）。 */
  const JUNK_NO = '9'.repeat(16)

  it('① delete 对「非空但非 20 位」的单号**不再本地抛错**，且照常发 POST …/batch/delete/', async () => {
    await batchOrderAction('delete', { order_nos: [JUNK_NO] })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/batch/delete/',
      data: { order_nos: [JUNK_NO] },
    })
  })

  it('② assign 对同一个 16 位单号**仍然抛**那条 20 位文案（「只放开删除」的边界，⛔ 不是漏改）', async () => {
    await expect(
      batchOrderAction('assign', { order_nos: [JUNK_NO], user_id: 1 }),
    ).rejects.toThrow(`订单号格式不正确（应为 20 位数字）：${JUNK_NO}`)
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('③ delete 对**空串**仍然抛（后端 `parse_order_nos` 也拒空串 ⇒ 不是「delete 什么都收」）', async () => {
    await expect(batchOrderAction('delete', { order_nos: [''] })).rejects.toThrow(
      '订单号格式不正确（应为 20 位数字）：',
    )
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('④ delete 对**非字符串**仍然抛（后端契约是「非空**字符串**」）', async () => {
    await expect(
      batchOrderAction('delete', { order_nos: [123 as unknown as string] }),
    ).rejects.toThrow('订单号格式不正确（应为 20 位数字）：123')
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})
