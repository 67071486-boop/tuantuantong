import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 店铺对接**五个**出口的**传输契约**（spec §6.1）。形状照 `api/__tests__/orderSources.spec.ts`。⚠️ 这个文件**不在 brief 的 Files 清单里**（brief 只列了页面 spec）。加它的理由与`orderSources.spec.ts:35-52` 完全相同：`page_size=200` 写在唯一的取数出口里，而页面 spec把整个 api 模块 `vi.mock` 掉了 ⇒ 没有这一条，「漏带 page_size → 第 21 条静默消失」（本线的评审 Critical 1 同族）在门禁里**完全没有守卫**。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  createShopBinding,
  deleteShopBinding,
  fetchPlatformConfig,
  fetchShopBindings,
  updateShopBinding,
} from '../shopBindings'

const mockedRequest = vi.mocked(request)
const URL = '/api/business/shop-bindings/'

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
})

describe('fetchShopBindings：本页表格的数据源', () => {
  it('GET 集合路径，显式带 page=1 与 page_size=200', async () => {
    await fetchShopBindings()

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: URL,
      params: { page: 1, page_size: 200 },
    })
  })

  it('`page_size` 是接口上限 200，不是后端默认的 20（不带就静默截断到 20 条）', async () => {
    await fetchShopBindings()

    const config = mockedRequest.mock.calls[0]![0] as { params: Record<string, number> }
    expect(config.params.page_size).toBe(200)
    expect(config.params.page_size).not.toBe(20)
  })

  it('返回**整页**（items/total/…），不是已被解包的数组', async () => {
    const page = { items: [], total: 0, page: 1, page_size: 200 }
    mockedRequest.mockResolvedValue(page)

    await expect(fetchShopBindings()).resolves.toBe(page)
  })
})

/** 平台级 AppId 的独立出口（第五个出口）。⚠️ 它**必须**有这一条：页面里 `fetchPlatformConfig().catch(() => null)` 让端点失败**按设计静默**⇒ URL 打错（或路径漂移）在生产上只会表现为「弹层 AppId 又空了」，与本提交要修的 bug 同形，而页面 spec 把整个 api 模块 `vi.mock` 掉了、抓不到。判据：URL 落到平台配置路径（不是集合路径）、方法 `get`，并钉住**解包后的返回值**（`request()` 已把信封拆掉，这里拿到的是 `data` 本体）。 */
describe('fetchPlatformConfig：平台级 AppId（与「行」解耦）', () => {
  it('GET `…/platform-config/`，返回解包后的 `platform_app_id`（字面值）', async () => {
    mockedRequest.mockResolvedValue({ platform_app_id: 'app-literal-42' })

    await expect(fetchPlatformConfig()).resolves.toEqual({ platform_app_id: 'app-literal-42' })
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: `${URL}platform-config/`,
    })
  })
})

describe('写出口：方法与 URL', () => {
  it('新增 → POST 集合路径，载荷原样进 `data`（含 `order_source: null`＝「不设置」）', async () => {
    await createShopBinding({
      platform: 20,
      shop_name: '我的店',
      order_source: null,
      access_token: 'tok',
      remark: '',
    })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: URL,
      data: {
        platform: 20,
        shop_name: '我的店',
        order_source: null,
        access_token: 'tok',
        remark: '',
      },
    })
  })

  it('编辑 → **PATCH** 详情路径（`urls.py` 只注册了 GET/POST/PATCH/DELETE，**没有 PUT**）', async () => {
    await updateShopBinding(7, { platform: 20, remark: '只改备注' })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'patch',
      url: `${URL}7/`,
      data: { platform: 20, remark: '只改备注' },
    })
    expect(mockedRequest.mock.calls[0]![0].method).not.toBe('put')
  })

  it('删除 → DELETE 详情路径', async () => {
    await deleteShopBinding(7)

    expect(mockedRequest).toHaveBeenCalledWith({ method: 'delete', url: `${URL}7/` })
  })

  it('`order_source: null` 必须真发出去（清掉绑定 = 回到「不设置」，不是「没填」，E-0021 同族）', async () => {
    await updateShopBinding(7, { platform: 20, order_source: null })

    const data = (mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }).data
    expect(Object.keys(data)).toEqual(['platform', 'order_source'])
    expect(data.order_source).toBeNull()
  })
})
