import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  fetchRecognitionSettings,
  RECOGNITION_SETTINGS_URL,
  updateRecognitionKeywords,
} from '../recognition'

const mockedRequest = vi.mocked(request)

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({ items: [] })
})

/** 取第 1 次请求的配置（判 URL / 方法 / 载荷三个出口）。
 * ⚠️ 用**字面值**钉 URL 与载荷（E-0024：不拿导出常量去断言它自己）——
 * `RECOGNITION_SETTINGS_URL` 只在下面单独一条里钉死字面串，不在别处自指。 */
function firstCall(): { method?: string; url?: string; data?: unknown; params?: unknown } {
  const call = mockedRequest.mock.calls[0]
  expect(call, '没有发出请求').toBeDefined()
  return call![0] as { method?: string; url?: string; data?: unknown; params?: unknown }
}

describe('recognition API 出口（spec §7.1 / §5 的接口契约）', () => {
  it('集合路径就是后端注册的那一条字面串', () => {
    expect(RECOGNITION_SETTINGS_URL).toBe('/api/business/recognition-settings/')
  })

  it('取列表 → GET 集合路径，不带任何分页参数（14 行由后端固定顺序返回）', async () => {
    await fetchRecognitionSettings()

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/recognition-settings/',
    })
    expect(firstCall().params).toBeUndefined()
  })

  it('取列表返回 `{items}`（`data` 的形状逐字是契约里的那个）', async () => {
    const page = {
      items: [{ item_code: 'price', name: '接单价格', keywords: '价格,接单价,金额' }],
    }
    mockedRequest.mockResolvedValue(page)

    await expect(fetchRecognitionSettings()).resolves.toBe(page)
  })

  it('改一行 → PATCH 详情路径（`<item_code>/` 带尾斜杠），载荷只有 `{keywords}`', async () => {
    await updateRecognitionKeywords('price', '价格,总价')

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'patch',
      url: '/api/business/recognition-settings/price/',
      data: { keywords: '价格,总价' },
    })
  })

  it('`keywords: ""` 必须真发出去（清空是合法覆盖，不是「没填」，丢了它清不掉）', async () => {
    await updateRecognitionKeywords('price', '')

    const config = firstCall()
 // 键集合判据：`toEqual` 会忽略 `undefined` 属性 ⇒ 「带了 keywords: undefined」也会绿（同
 // `orderSources.spec.ts` 记的坑），所以这里同时断键名与值。
    expect(Object.keys(config.data as Record<string, unknown>)).toEqual(['keywords'])
    expect(config.data).toEqual({ keywords: '' })
  })

  it('`item_code` 进 URL 前要编码（它直接来自后端行数据）', async () => {
    await updateRecognitionKeywords('a b/c', '价格')

    expect(firstCall().url).toBe('/api/business/recognition-settings/a%20b%2Fc/')
  })

  it('改一行返回保存后的整行（`data` 就是那一行）', async () => {
    const row = { item_code: 'price', name: '接单价格', keywords: '总价' }
    mockedRequest.mockResolvedValue(row)

    await expect(updateRecognitionKeywords('price', '总价')).resolves.toBe(row)
  })
})
