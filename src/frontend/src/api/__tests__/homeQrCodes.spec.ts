import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  HOME_QR_CODES_URL,
  HOME_QR_TICKET_URL,
  createHomeQrCode,
  deleteHomeQrCode,
  fetchHomeQrCodes,
  issueHomeQrTicket,
  updateHomeQrCode,
  type HomeQrRow,
} from '../homeQrCodes'

/** 首页二维码横幅接口层（`api/homeQrCodes.ts`）的**请求形状**测试。钉的是真实出口：**方法 + URL + 载荷**。后端实现逐条核过（`apps/business/api/qr_code_views.py`、`apps/business/urls.py`）：- 列表 GET 不带任何参数（登录即可、按持点分档是服务端 queryset 的事）；- 票据 POST 只带 `{ext}`；- 写入走 PATCH（部分更新：换图 / 启停 / 排序各带各的键）⇒ ⛔ 不发全量 PUT。 */
const mockedRequest = vi.mocked(request)

/** 一行合成夹具（值互不相同：串行断言时能分辨拿没拿错行）。 */
const row = (id: number, overrides: Partial<HomeQrRow> = {}): HomeQrRow => ({
  id,
  kind: 'qq_group',
  game_id: 100 + id,
  game_name: `游戏${id}`,
  media_key: `demo/users/7/home-qr.abc${id}.jpg`,
  media_url: `https://media.example.com/signed-${id}`,
  sort_order: id,
  is_enabled: true,
  created_by: 'hr01',
  created_at: '2026-10-19T10:00:00+08:00',
  updated_at: '2026-10-19T10:00:00+08:00',
  ...overrides,
})

type Call = { method?: string; url?: string; params?: unknown; data?: unknown }
const calls: Call[] = []

beforeEach(() => {
  calls.length = 0
  mockedRequest.mockReset()
  mockedRequest.mockImplementation(async (config: Call) => {
    calls.push(config)
    return { items: [], total: 0 }
  })
})

describe('列表与票据', () => {
  it('GET 列表：方法 / URL / 零参数（分档在服务端，前端不发明筛选参数）', async () => {
    await fetchHomeQrCodes()
    expect(calls).toEqual([{ method: 'GET', url: HOME_QR_CODES_URL }])
    expect(HOME_QR_CODES_URL).toBe('/api/business/home/qr-codes/')
  })

  it('POST 票据：URL 是 …/media/ticket/、载荷只有 `{ext}`', async () => {
    await issueHomeQrTicket('jpg')
    expect(calls).toEqual([
      { method: 'POST', url: HOME_QR_TICKET_URL, data: { ext: 'jpg' } },
    ])
    expect(HOME_QR_TICKET_URL).toBe('/api/business/home/qr-codes/media/ticket/')
  })
})

describe('写入：POST 新增 / PATCH 部分更新 / DELETE 硬删', () => {
  it('POST 新增群码：载荷 = `{kind, game_id, media_key, sort_order}`', async () => {
    mockedRequest.mockImplementation(async (config: Call) => {
      calls.push(config)
      return row(1)
    })
    const created = await createHomeQrCode({
      kind: 'qq_group',
      game_id: 101,
      media_key: 'demo/users/7/home-qr.abc1.jpg',
      sort_order: 1,
    })
    expect(created.id).toBe(1)
    expect(calls[0]).toEqual({
      method: 'POST',
      url: HOME_QR_CODES_URL,
      data: { kind: 'qq_group', game_id: 101, media_key: 'demo/users/7/home-qr.abc1.jpg', sort_order: 1 },
    })
  })

  it('POST 新增固定格：不带 `game_id` 键（undefined 的键不会出现在 JSON 里）', async () => {
    await createHomeQrCode({ kind: 'qq_bot', media_key: 'demo/users/7/home-qr.bot.jpg' })
    expect(calls[0]!.data).toEqual({ kind: 'qq_bot', media_key: 'demo/users/7/home-qr.bot.jpg' })
  })

  it('PATCH 只带本次要改的键：换图不带 is_enabled、启停不带 media_key', async () => {
    await updateHomeQrCode(3, { media_key: 'demo/users/7/home-qr.new.jpg' })
    expect(calls[0]).toEqual({
      method: 'PATCH',
      url: `${HOME_QR_CODES_URL}3/`,
      data: { media_key: 'demo/users/7/home-qr.new.jpg' },
    })

    await updateHomeQrCode(3, { is_enabled: false })
    expect(calls[1]).toEqual({
      method: 'PATCH',
      url: `${HOME_QR_CODES_URL}3/`,
      data: { is_enabled: false },
    })

    // 排序：`{sort_order}`（数字小的在前）
    await updateHomeQrCode(3, { sort_order: 0 })
    expect(calls[2]!.data).toEqual({ sort_order: 0 })
  })

  it('DELETE：URL 带 id、无载荷', async () => {
    await deleteHomeQrCode(9)
    expect(calls).toEqual([{ method: 'DELETE', url: `${HOME_QR_CODES_URL}9/` }])
  })
})
