import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  fetchAllMyOrders,
  fetchMyOrders,
  MY_ORDERS_PAGE_SIZE,
  submitMyOrder,
  toMyOrderParams,
} from '../myOrders'

const mockedRequest = vi.mocked(request)

/** 20 位数字（后端 `lookup_value_regex = r"\d{20}"` 的唯一合法形状）。 */
const NO = '20260920213215233029'

function row(orderNo: string): { order_no: string } {
  return { order_no: orderNo }
}

beforeEach(() => {
  mockedRequest.mockReset()
})

describe('toMyOrderParams：「传没传」判据，0 不被当空值丢掉', () => {
  it('全空 → 空对象（不发多余参数）', () => {
    expect(toMyOrderParams({})).toEqual({})
  })

  it('page:0 / page_size:0 原样发出去（让后端明确拒绝，不在参数层静默改写）', () => {
    expect(toMyOrderParams({ page: 0, page_size: 0 })).toEqual({ page: 0, page_size: 0 })
  })
})

describe('fetchMyOrders：列表真的走 GET /api/business/my-orders/', () => {
  it('方法 + URL + 分页参数逐项对上（URL 写字面值，不用实现里的常量自证）', async () => {
    mockedRequest.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
    await fetchMyOrders({ page: 2, page_size: 50 })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/my-orders/',
      params: { page: 2, page_size: 50 },
    })
  })

  it('不传参数时 params 是空对象（不会把 undefined 发给后端）', async () => {
    mockedRequest.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 })
    await fetchMyOrders()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/my-orders/',
      params: {},
    })
  })
})

describe('fetchAllMyOrders：一页够用就只发一次；不够就按 page 递增取完', () => {
  it('total ≤ 一页 → 恰 1 次请求，page_size = 200（后端 max_page_size）', async () => {
    mockedRequest.mockResolvedValue({
      items: [row(NO)],
      total: 1,
      page: 1,
      page_size: MY_ORDERS_PAGE_SIZE,
    })
    const items = await fetchAllMyOrders()
    expect(items).toHaveLength(1)
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/my-orders/',
      params: { page: 1, page_size: 200 },
    })
  })

  it('total 跨页 → 第 2 次请求带 page=2，结果按顺序拼接', async () => {
    mockedRequest
      .mockResolvedValueOnce({
        items: [row('20260920213215233001'), row('20260920213215233002')],
        total: 3,
        page: 1,
        page_size: MY_ORDERS_PAGE_SIZE,
      })
      .mockResolvedValueOnce({
        items: [row('20260920213215233003')],
        total: 3,
        page: 2,
        page_size: MY_ORDERS_PAGE_SIZE,
      })
    const items = await fetchAllMyOrders()
    expect(items.map((i) => i.order_no)).toEqual([
      '20260920213215233001',
      '20260920213215233002',
      '20260920213215233003',
    ])
    expect(mockedRequest).toHaveBeenNthCalledWith(2, {
      method: 'get',
      url: '/api/business/my-orders/',
      params: { page: 2, page_size: 200 },
    })
  })

  it('后端忽略 page（重复回同一页）时**收敛**而不是空转：total 达到即停', async () => {
    mockedRequest.mockResolvedValue({
      items: [row('20260920213215233001'), row('20260920213215233002')],
      total: 4,
      page: 1,
      page_size: MY_ORDERS_PAGE_SIZE,
    })
    const items = await fetchAllMyOrders()
 // 每轮 +2，第 2 轮就 ≥ total（4）⇒ 恰 2 次请求（不是 MAX_PAGES 次）。
    expect(mockedRequest).toHaveBeenCalledTimes(2)
    expect(items).toHaveLength(4)
  })
})

describe('submitMyOrder：提交完单', () => {
  it('POST /api/business/orders/<order_no>/submit/，**不带请求体**（后端 submit 不读 request.data）', async () => {
    mockedRequest.mockResolvedValue({ order_no: NO, status: 'PENDING_REVIEW' })
    await submitMyOrder(NO)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/orders/20260920213215233029/submit/',
    })
  })

  it('单号形状不对时**不发请求**（抛前端自己的 Error）', async () => {
    await expect(submitMyOrder('abc')).rejects.toThrow('订单号格式不正确')
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})
