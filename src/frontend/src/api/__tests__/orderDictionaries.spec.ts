import { beforeEach, expect, it, vi } from 'vitest'
import { fetchMe } from '../auth'
import { fetchAllGames, fetchGames, fetchServers } from '../config'
import { AuthExpiredError, HttpError } from '../errors'
import { request } from '../http'
import { fetchOrderDictionaries } from '../orderDictionaries'
import { fetchOrderSources } from '../orderSources'

vi.mock('../auth', () => ({ fetchMe: vi.fn() }))
vi.mock('../config', () => ({ fetchAllGames: vi.fn(), fetchGames: vi.fn(), fetchServers: vi.fn() }))
vi.mock('../http', () => ({ request: vi.fn() }))
vi.mock('../orderSources', () => ({ fetchOrderSources: vi.fn() }))

const data = {
  games: [{ id: 1, name: '甲游' }, { id: 2, name: '乙游' }],
  servers: [{ id: 12, game: 1, name: '二区' }],
  order_sources: [{ id: 31, name: '渠道一' }],
}

beforeEach(() => {
  vi.resetAllMocks()
})

it('管理订单字典直接返回三个下拉', async () => {
  vi.mocked(request).mockResolvedValue(data)
  expect(await fetchOrderDictionaries()).toEqual(data)
  expect(request).toHaveBeenCalledExactlyOnceWith({
    method: 'get',
    url: '/api/business/orders/dictionary-options/',
  })
  expect(fetchMe).not.toHaveBeenCalled()
})

it('兼容旧订单页每次按当前登录用户的游戏权限收窄', async () => {
  vi.mocked(request).mockResolvedValue(data)
  vi.mocked(fetchMe).mockResolvedValue({ game_ids: [2] } as Awaited<ReturnType<typeof fetchMe>>)
  expect(await fetchOrderDictionaries({ scopedGames: true })).toEqual({
    ...data,
    games: [{ id: 2, name: '乙游' }],
  })
})

it('旧后端尚无新端点时仍可读取原有字典', async () => {
  vi.mocked(request).mockRejectedValue(new HttpError(404, '未找到'))
  vi.mocked(fetchGames).mockResolvedValue([])
  vi.mocked(fetchServers).mockResolvedValue([])
  vi.mocked(fetchOrderSources).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
  expect(await fetchOrderDictionaries({ scopedGames: true })).toEqual({
    games: [], servers: [], order_sources: [],
  })
  expect(fetchGames).toHaveBeenCalledOnce()
  expect(fetchAllGames).not.toHaveBeenCalled()
})

it.each([new AuthExpiredError('会话已变更'), new HttpError(403, '无订单权限')])(
  '认证或授权被拒绝时保留错误，不再请求旧字典',
  async (error) => {
    vi.mocked(request).mockRejectedValue(error)
    await expect(fetchOrderDictionaries()).rejects.toBe(error)
    expect(fetchAllGames).not.toHaveBeenCalled()
    expect(fetchOrderSources).not.toHaveBeenCalled()
  },
)
