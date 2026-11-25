import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  createOrderSource,
  createOrderSourceGroup,
  fetchAllOrderSources,
  fetchOrderSourceGroups,
  fetchOrderSources,
  removeOrderSource,
  removeOrderSourceGroup,
  toOrderSourceGroupPayload,
  toOrderSourcePayload,
  updateOrderSource,
  updateOrderSourceGroup,
} from '../orderSources'

const mockedRequest = vi.mocked(request)

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
})

/** 取第 1 次请求的 `data`。⚠️ 「空值不发」必须用**键集合**判，不能只写 `toEqual({...})`：`toEqual` 系断言**忽略值为`undefined` 的属性** ⇒ 「过滤掉 remark」与「带着 `remark: undefined` 发出去」在那种写法下**都会绿**（恒真用例）。同 `config-crud.spec.ts` 里那条注释记的坑。 */
function sentData(): Record<string, unknown> {
  const call = mockedRequest.mock.calls[0]
  expect(call, '没有发出请求').toBeDefined()
  return (call![0] as { data: Record<string, unknown> }).data
}

describe('fetchOrderSources：列表即下拉数据源（同一个端点）', () => {
  it('GET order-sources，显式带 page=1 与 page_size=200', async () => {
    await fetchOrderSources()

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/order-sources/',
      params: { page: 1, page_size: 200 },
    })
  })

  it('page_size 是接口上限 200，不是后端默认的 20（不带就静默截断到 20 条）', async () => {
    // 这条是评审 Critical 1 的守卫：删掉 `params` 会红（`params` 变 undefined ⇒ 抛错/断言失败），
    // 把它写成 20 也红。判别力不依赖别的用例。
    await fetchOrderSources()

    const config = mockedRequest.mock.calls[0]![0] as { params: Record<string, number> }
    expect(config.params.page_size).toBe(200)
    expect(config.params.page_size).not.toBe(20)
  })

  it('返回整页（items/total/…），不是已被解包的数组——要 total 的调用方也能用', async () => {
    const page = { items: [], total: 0, page: 1, page_size: 200 }
    mockedRequest.mockResolvedValue(page)

    await expect(fetchOrderSources()).resolves.toBe(page)
  })
})

it('加载供新增分组选取的来源时，继续取后面的分页', async () => {
  const first = { id: 1, name: '第一页来源' }
  const last = { id: 201, name: '第二页来源' }
  mockedRequest.mockResolvedValueOnce({ items: [first], total: 201, page: 1, page_size: 200 })
  mockedRequest.mockResolvedValueOnce({ items: [last], total: 201, page: 2, page_size: 200 })

  await expect(fetchAllOrderSources()).resolves.toEqual([first, last])
  expect(mockedRequest).toHaveBeenNthCalledWith(2, {
    method: 'get',
    url: '/api/business/order-sources/',
    params: { page: 2, page_size: 200 },
  })
})

describe('写出口：URL / 方法 / 请求体', () => {
  it('新增 → POST 集合路径，四列原样进载荷（含 order_by: 0 与 shop_type: 20）', async () => {
    await createOrderSource({ name: '闲鱼', remark: '备注', order_by: 0, shop_type: 20 })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/order-sources/',
      data: { name: '闲鱼', remark: '备注', order_by: 0, shop_type: 20 },
    })
  })

  it('新增：order_by: 0 与 shop_type: 0 必须真发出去（0 是 R5 的合法值，不是「没填」，E-0021）', async () => {
    await createOrderSource({ name: '闲鱼', order_by: 0, shop_type: 0 })

    expect(Object.keys(sentData())).toEqual(['name', 'order_by', 'shop_type'])
    expect(sentData()).toEqual({ name: '闲鱼', order_by: 0, shop_type: 0 })
  })

  it('新增：null / undefined 的列不发（空值不发，让后端用默认值）——**`group` 是唯一例外**（见下一组）', async () => {
    await createOrderSource({ name: '闲鱼', remark: null, shop_type: undefined })

    expect(Object.keys(sentData())).toEqual(['name'])
    expect(sentData()).toEqual({ name: '闲鱼' })
  })

  it('编辑 → PATCH 详情路径（带 id），只带改动字段', async () => {
    await updateOrderSource(7, { name: '改名' })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'patch',
      url: '/api/business/order-sources/7/',
      data: { name: '改名' },
    })
  })

  it('编辑：清空备注（remark: ""）必须发出去——丢了它后端收到不到空串，备注永远删不掉', async () => {
    await updateOrderSource(7, { name: '改名', remark: '', order_by: 0, shop_type: 0 })

    expect(Object.keys(sentData())).toEqual(['name', 'remark', 'order_by', 'shop_type'])
    expect(sentData()).toEqual({ name: '改名', remark: '', order_by: 0, shop_type: 0 })
  })

  it('编辑：order_by: 0 不能被当成空值丢掉', async () => {
    await updateOrderSource(7, { name: '改名', remark: null, order_by: 0 })

    expect(Object.keys(sentData())).toEqual(['name', 'order_by'])
    expect(sentData().order_by).toBe(0)
  })

  it('删除 → DELETE 详情路径（带 id）', async () => {
    await removeOrderSource(7)

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'delete',
      url: '/api/business/order-sources/7/',
    })
  })
})

describe('toOrderSourcePayload：纯函数口径（空值不发，0 与空串留下）', () => {
  it('undefined / null 丢掉，`0` 与 `""` 留下', () => {
    const built = toOrderSourcePayload({ name: '甲', remark: '', order_by: 0, shop_type: null })

    expect(Object.keys(built)).toEqual(['name', 'remark', 'order_by'])
    expect(built).toEqual({ name: '甲', remark: '', order_by: 0 })
  })

  it('四个键都有真实值时原样返回（不改值、不补键）', () => {
    expect(
      toOrderSourcePayload({ name: '甲', remark: '说明', order_by: -3, shop_type: 70 }),
    ).toEqual({ name: '甲', remark: '说明', order_by: -3, shop_type: 70 })
  })
})

/* ────────── B1：`group: null` 必须透传（计划 §5 的 B1）────────── */

describe('`toOrderSourcePayload` / 写出口：只有 group 透传 null', () => {
  it('`group: null` 进载荷（= 改成未分组）；⛔ 丢掉它会让这次保存静默失败', async () => {
    await updateOrderSource(7, { name: '改名', group: null })

    expect(Object.keys(sentData())).toEqual(['name', 'group'])
    expect(sentData()).toEqual({ name: '改名', group: null })
    expect(sentData().group).toBeNull()
  })

  it('不传 group 时那个键**不出现**（PATCH 不该无端清空分组）', () => {
    const built = toOrderSourcePayload({ name: '甲' })

    expect(Object.keys(built)).toEqual(['name'])
    expect('group' in built).toBe(false)
  })

  it('`remark: null` **仍被丢弃**（后端 allow_null=False，送了就 1000）', () => {
    const built = toOrderSourcePayload({ name: '甲', remark: null, group: null })

    expect(Object.keys(built)).toEqual(['name', 'group'])
    expect(built).toEqual({ name: '甲', group: null })
  })

  it('`group` 有值时原样发（数字，不是字符串）', () => {
    expect(toOrderSourcePayload({ name: '甲', group: 4 }).group).toBe(4)
  })
})

/* ────────── 公司店铺分组：字典与三个写出口（计划 §3.2）────────── */

describe('fetchOrderSourceGroups：字典出口', () => {
  it('GET order-source-groups，显式带 page=1 与 page_size=200（默认 20 会静默截断）', async () => {
    await fetchOrderSourceGroups()

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/order-source-groups/',
      params: { page: 1, page_size: 200 },
    })
  })
})

describe('分组写出口：URL / 方法 / 请求体', () => {
  it('新增 → POST 集合路径；`order_by: 0` 照发（E-0021 的族）', async () => {
    await createOrderSourceGroup({ name: '甲组', order_by: 0 })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/order-source-groups/',
      data: { name: '甲组', order_by: 0 },
    })
  })

  it('改名 → PATCH 详情路径', async () => {
    await updateOrderSourceGroup(4, { name: '乙组' })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'patch',
      url: '/api/business/order-source-groups/4/',
      data: { name: '乙组' },
    })
  })

  it('删除 → DELETE 详情路径', async () => {
    await removeOrderSourceGroup(4)

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'delete',
      url: '/api/business/order-source-groups/4/',
    })
  })

  it('`toOrderSourceGroupPayload`：只丢 undefined / null（0 与空串留下）', () => {
    const built = toOrderSourceGroupPayload({ name: '甲组', order_by: 0 })
    expect(built).toEqual({ name: '甲组', order_by: 0 })
    expect(
      Object.keys(toOrderSourceGroupPayload({ name: '甲组', order_by: null })),
    ).toEqual(['name'])
  })
})
