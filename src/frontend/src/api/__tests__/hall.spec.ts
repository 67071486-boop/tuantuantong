import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  claimOrder,
  fetchHall,
  throwToHall,
  toHallParams,
  withdrawThrow,
} from '../hall'

const mockedRequest = vi.mocked(request)
const NO = '12345678901234567890'

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({})
})

describe('toHallParams：空值不发送，「0」必须留下', () => {
  it('空串 / undefined / null 一律去掉', () => {
    expect(
      toHallParams({ game: null, server: undefined, keyword: '', project_text: '  ' }),
    ).toEqual({})
  })

  it('price_min/price_max 的 0 与负数都是**有意义的值**，不许被真值判断丢掉（D-0015 / E-0021）', () => {
    expect(toHallParams({ price_min: 0, price_max: -1 })).toEqual({ price_min: 0, price_max: -1 })
  })

  it('page/page_size 原样保留（列表页把它交给 layui 的 pageName/limitName，这里是等价出口）', () => {
    expect(toHallParams({ page: 2, page_size: 20 })).toEqual({ page: 2, page_size: 20 })
  })

  it('保留真实值（六个筛选参数都在类型里）', () => {
    expect(
      toHallParams({
        game: 3,
        server: 7,
        project_text: '深渊满星',
        keyword: '角色名',
        price_min: '10.00',
        price_max: '999.99',
      }),
    ).toEqual({
      game: 3,
      server: 7,
      project_text: '深渊满星',
      keyword: '角色名',
      price_min: '10.00',
      price_max: '999.99',
    })
  })
})

describe('端点与载荷', () => {
  it('大厅列表：GET /api/business/hall/，params 经过空值过滤', async () => {
    await fetchHall({ game: 1, keyword: '', page: 1, page_size: 20 })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/hall/',
      params: { game: 1, page: 1, page_size: 20 },
    })
  })

  it('fetchHall() 不带参数时 params 是空对象（不是 undefined）', async () => {
    await fetchHall()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/hall/',
      params: {},
    })
  })

  it('抢单：POST /api/business/hall/<单号>/claim/，无 body', async () => {
    await claimOrder(NO)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/hall/${NO}/claim/`,
    })
  })

  it('抛单：POST /api/business/orders/<单号>/throw-to-hall/，带价时 data.price 是字符串', async () => {
    await throwToHall(NO, '100.00')
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/throw-to-hall/`,
      data: { price: '100.00' },
    })
  })

  it('抛单价为 "0" 时**必须发出去**（0 是合法金额，D-0015）', async () => {
    await throwToHall(NO, '0')
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/throw-to-hall/`,
      data: { price: '0' },
    })
  })

  it('抛单价为空（undefined / null / 空串）→ data 是空对象（后端用订单已有默认价）', async () => {
    await throwToHall(NO)
    await throwToHall(NO, null)
    await throwToHall(NO, '  ')
    expect(mockedRequest).toHaveBeenCalledTimes(3)
    for (const [config] of mockedRequest.mock.calls) {
 // ⚠️ 这里**不能用 `toMatchObject`**：嵌套的空对象是**恒真断言**——vitest 4 的
 // `isObjectWithKeys({})` 对任何对象都真（`Object.keys({}).every(...)` 空集恒真），
 // 于是「空值也把价发出去」的 bug（如 `data: { price: '0' }`）照样绿。
 // 本用例的全部意义就是判「空值不发」，断言必须**能红**（同型教训：E-0009 / E-0024）
 // ⇒ 用 `toEqual` 全等（多一个 key 即红），再显式钉死 `data` 的键数为 0。
      expect(config).toEqual({
        method: 'post',
        url: `/api/business/orders/${NO}/throw-to-hall/`,
        data: {},
      })
      expect(Object.keys(config.data as Record<string, unknown>)).toHaveLength(0)
    }
  })

  it('撤回抛单：POST /api/business/orders/<单号>/withdraw/', async () => {
    await withdrawThrow(NO)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/orders/${NO}/withdraw/`,
    })
  })
})

describe('订单号形状校验：形状不对**一次请求都不发**', () => {
 // 形状校验只覆盖**三个**带单号的动作（抢单 / 抛单 / 撤回）；`fetchHall` 不收单号，天然不在此列。
 // ⚠️ **这三个动作形状不对时的失败形状并不一样**（追加轮探针实测，别写成「不拦就是 HTML 404」）：
 // - **抢单**走 `/api/business/hall/<order_no>/claim/`——**显式 `path()` + 宽松
 // `<order_no:…>` 转换器**（`apps/business/urls.py:73`；`converters.py` 只排除控制字符）
 // ⇒ 畸形单号**照样匹配**，**回什么取决于闸门**（末轮探针各跑了一次）：
 // · 已授权（能过闸门）⇒ **200 + 业务码**
 // （`POST /api/business/hall/abc/claim/` ⇒ `200` + `application/json` +
 // `{"code":1002,"msg":"记录不存在或已被删除","data":null}`）；
 // · 已登录但无权限 ⇒ **403 + JSON**
 // （同路径、无节点账号 ⇒ `403` + `{"code":403,"msg":"您没有执行该操作的权限。","data":null}`）。
 // ⇒ **这两种身份下都不是 404 HTML**（路由已经匹配上了）。
 // - **抛单 / 撤回**走 router 生成的 `orders/{pk}/…`（吃 `lookup_value_regex = r"\d{20}"`）
 // ⇒ 形状不对连路由都匹配不上，回 **404 + HTML**，由 `http.ts` 的 `mapHttpStatus`
 // 归成 `HttpError(404, …)`（空 msg 走状态码兜底文案）。
 // ⇒ 前端拦一道对**两者都是省一次往返**，不是「不拦就没信封」。同族句子在本轮
 // `api/orders.ts` 的 `partialRefund` / `isOrderNoShape` docstring 里已按实测改写。
  it('三个动作（抢单 / 抛单 / 撤回）都先过形状，一次请求都不发', async () => {
    await expect(claimOrder('abc')).rejects.toThrow('订单号格式不正确（应为 20 位数字）：abc')
    await expect(throwToHall('abc', '1.00')).rejects.toThrow(
      '订单号格式不正确（应为 20 位数字）：abc',
    )
    await expect(withdrawThrow('abc')).rejects.toThrow(
      '订单号格式不正确（应为 20 位数字）：abc',
    )
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})
