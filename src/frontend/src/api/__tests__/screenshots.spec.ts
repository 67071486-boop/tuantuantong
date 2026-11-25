/** ⭐ **I-1（2026-09-25 评审修复）：`api/screenshots.ts` 五条端点的判据。**背景（独立评审的变异实验）：本模块此前**整份零覆盖**——把 5 条 URL 全改成`/api/WRONG/...` 之后全仓 vitest 仍然全绿。本文件把每条端点的**方法 + 路径 + 请求体键名 + 返回值透传**逐条钉死。⚠️ 判据一律写**字面值**（URL / method / body 键名都是手写的）：拿模块导出的常量去断言模块自己的行为是自指断言，常量被改坏时不会红（`AGENTS.md` §5-G / E-0024）。⚠️ `method` 断言写**大写**：本模块给 `request()` 的 config 就是大写（`'POST'` / `'GET'` / `'DELETE'`），而 `request` 在本文件里是**整份替身** ⇒记到的是原样入参（不像 `orders.ts` 那样传小写）。改一个字母就会红。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  claimScreenshot,
  deleteScreenshot,
  fetchScreenshots,
  requestScreenshotTicket,
  screenshotZipUrl,
  type ScreenshotItem,
  type ScreenshotTicket,
} from '../screenshots'

const mockedRequest = vi.mocked(request)
/** 合成单号（20 位数字，形状同后端 `Order.order_no`）。 */
const NO = '20260101000000000001'

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({})
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('ticket：POST …/screenshots/ticket（`{ext, img_type}`）', () => {
  it('路径无尾斜杠、方法 POST、body 恰好 `{ext, img_type}`（键名是 snake_case）', async () => {
    await requestScreenshotTicket(NO, 'png', 1)
    expect(mockedRequest).toHaveBeenCalledWith({
      url: `/api/business/orders/${NO}/screenshots/ticket`,
      method: 'POST',
      data: { ext: 'png', img_type: 1 },
    })
  })

  it('入参顺序是 (单号, 扩展名, 图片类型)——出网体里的值逐项对应', async () => {
    await requestScreenshotTicket(NO, 'jpg', 0)
    const cfg = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    // 逐项取**值**（不是钉常量）：`ext` 拿第二个形参、`img_type` 拿第三个。
    expect(cfg.data).toEqual({ ext: 'jpg', img_type: 0 })
  })

  it('返回值原样透传（`request()` 已解开信封 ⇒ 本层不再挑键）', async () => {
    const ticket: ScreenshotTicket = {
      screenshot_id: 11, key: 'demo/orders/x/y.jpg',
      upload_url: 'https://cos.example.invalid/bucket/y.jpg', expires_in: 600,
    }
    mockedRequest.mockResolvedValueOnce(ticket)
    await expect(requestScreenshotTicket(NO, 'jpg', 0)).resolves.toBe(ticket)
  })

  it('单号里的特殊字符走 `encodeURIComponent`（路径注入不进 URL）', async () => {
    await requestScreenshotTicket('a/../b c', 'jpg', 0)
    const cfg = mockedRequest.mock.calls[0]![0] as { url: string }
    expect(cfg.url).toBe('/api/business/orders/a%2F..%2Fb%20c/screenshots/ticket')
  })
})

describe('认领：POST …/screenshots（`screenshot_id` / `original_name` / `remark`）', () => {
  it('路径无尾斜杠、方法 POST、body 恰好那三个键（⛔ 不是按 `key` 认领）', async () => {
    await claimScreenshot(NO, {
      screenshot_id: 11, original_name: 'a.jpg', remark: '测试备注甲',
    })
    expect(mockedRequest).toHaveBeenCalledWith({
      url: `/api/business/orders/${NO}/screenshots`,
      method: 'POST',
      data: { screenshot_id: 11, original_name: 'a.jpg', remark: '测试备注甲' },
    })
  })

  it('备注为空串时**照样发**该键（`remark: ""` 不是「没传」）', async () => {
    await claimScreenshot(NO, { screenshot_id: 12, original_name: 'b.png', remark: '' })
    const cfg = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect(cfg.data).toEqual({ screenshot_id: 12, original_name: 'b.png', remark: '' })
  })

  it('返回值原样透传（认领回的是 `{id}`）', async () => {
    mockedRequest.mockResolvedValueOnce({ id: 11 })
    await expect(
      claimScreenshot(NO, { screenshot_id: 11, original_name: 'a.jpg', remark: '' }),
    ).resolves.toEqual({ id: 11 })
  })
})

describe('列表：GET …/screenshots', () => {
  it('路径无尾斜杠、方法 GET、**不带** data / params（后端不分页）', async () => {
    await fetchScreenshots(NO)
    expect(mockedRequest).toHaveBeenCalledWith({
      url: `/api/business/orders/${NO}/screenshots`,
      method: 'GET',
    })
  })

  it('返回值原样透传（`{items, total}`，本层不裁键）', async () => {
    const item: ScreenshotItem = {
      id: 1, img_type: 0, url: 'https://cos.example.invalid/x.jpg',
      original_name: 'a.jpg', size_bytes: 3, remark: '', uploaded_by: 7,
      created_at: '2026-09-25T10:00:00+08:00',
    }
    mockedRequest.mockResolvedValueOnce({ items: [item], total: 1 })
    await expect(fetchScreenshots(NO)).resolves.toEqual({ items: [item], total: 1 })
  })
})

describe('删除单张：DELETE /api/business/orders/screenshots/<id>', () => {
  it('路径是**不带单号**的 `/orders/screenshots/<id>`、方法 DELETE', async () => {
    await deleteScreenshot(42)
    expect(mockedRequest).toHaveBeenCalledWith({
      url: '/api/business/orders/screenshots/42',
      method: 'DELETE',
    })
  })

  it('id 原样进路径（不是订单号，也不 encode）', async () => {
    await deleteScreenshot(7)
    const cfg = mockedRequest.mock.calls[0]![0] as { url: string }
    expect(cfg.url).toBe('/api/business/orders/screenshots/7')
  })
})

/** 打包下载**不走 `request()`**（`window.open` 用）⇒ 判据打在**拼出来的 URL** 上。
 * ⚠️ 必须把 `VITE_API_BASE` 的两种取值都钉住：只钉空值那一半时，删掉 `base` 前缀的
 * 拼接不会有任何测试变红（部署在子路径下就会 404）。 */
describe('打包下载 URL：`GET …/screenshots/zip`（`window.open` 用，不走 request）', () => {
  it('无 `VITE_API_BASE` ⇒ 裸路径、带尾 `/zip`', () => {
    vi.stubEnv('VITE_API_BASE', '')
    expect(screenshotZipUrl(NO)).toBe(`/api/business/orders/${NO}/screenshots/zip`)
  })

  it('有 `VITE_API_BASE` ⇒ 前缀原样拼在前面', () => {
    vi.stubEnv('VITE_API_BASE', '/sub')
    expect(screenshotZipUrl(NO)).toBe(`/sub/api/business/orders/${NO}/screenshots/zip`)
  })

  it('这条路径**一次 `request()` 都不发**（它是给 `window.open` 的字符串）', () => {
    screenshotZipUrl(NO)
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})
