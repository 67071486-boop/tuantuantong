import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  fetchAllGames,
  fetchCategories,
  fetchGames,
  fetchProjects,
  fetchServers,
} from '../config'

const mockedRequest = vi.mocked(request)

function pageOf(items: unknown[]): unknown {
  return { items, total: items.length, page: 1, page_size: 200 }
}

/** 全量游戏字典（`GET /api/business/games/` 的响应体）。三款，用来分辨「过滤后」与「全量」。 */
const ALL_GAMES = [
  { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
  { id: 2, name: '崩铁', sort_order: 0, is_active: true, created_at: '' },
  { id: 3, name: '绝区零', sort_order: 0, is_active: true, created_at: '' },
]

/** 按 URL 分流的桩：`/api/auth/me` 回给定的 `game_ids`（本项目 Task 1 给该端点加的字段），`/api/business/games/` 回**全量**三款。⚠️ 必须按 URL 分流：两条路径过的是同一个 `request`，不分流的话 `fetchGames()` 会拿着`/auth/me` 的载荷当游戏列表（或反过来），断言的就不是被测的东西了。 */
function stubAuthAndGames(gameIds: number[]): void {
  mockedRequest.mockImplementation((async (config: { url?: string }) => {
    if (config.url === '/api/auth/me') return { user: { game_ids: gameIds } }
    if (config.url === '/api/business/games/') return pageOf(ALL_GAMES)
    throw new Error(`测试里没接这个 URL：${config.url}`)
  }) as never)
}

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue(pageOf([]))
})

describe('配置接口封装', () => {
  it('fetchGames 走 /api/business/games/，page_size 取上限 200', async () => {
    stubAuthAndGames([1, 2, 3])
    await fetchGames()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/games/',
      params: { page_size: 200 },
    })
  })

  it('fetchServers(7) 带 ?game=7 —— 走 Task 1 的服务端过滤，不拉全量再本地过滤', async () => {
    await fetchServers(7)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/servers/',
      params: { game: 7, page_size: 200 },
    })
  })

  it('fetchServers(null) 不带 game', async () => {
    await fetchServers(null)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/servers/',
      params: { page_size: 200 },
    })
  })

  it('fetchCategories(9) 带 ?game=9', async () => {
    await fetchCategories(9)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/categories/',
      params: { game: 9, page_size: 200 },
    })
  })

  it('fetchProjects(3) 带 ?category=3', async () => {
    await fetchProjects(3)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/projects/',
      params: { category: 3, page_size: 200 },
    })
  })

  it('返回的是 data.items，不是整个分页载荷', async () => {
    mockedRequest.mockResolvedValue(pageOf([{ id: 1, name: '原神' }]))
    await expect(fetchAllGames()).resolves.toEqual([{ id: 1, name: '原神' }])
  })
})

/** Task 5 的三条钉子（数据页下拉按 `game_ids` 收窄；配置面仍全量）。
 * 夹具刻意让「全量」= 3 款、而用户只持 1–2 款：只差一个元素的夹具区分不出
 * 「过滤」与「不过滤」（执行期修订 1 第 2 条的可复用教训）。 */
describe('游戏下拉按当前用户的 game_ids 收窄（Task 5）', () => {
  it('① game_ids 非空 ⇒ fetchGames() 只回那几个（钉字面 id 列表 [1, 3]）', async () => {
    stubAuthAndGames([1, 3])
    const games = await fetchGames()
    expect(games.map((g) => g.id)).toEqual([1, 3])
    expect(games.map((g) => g.name)).toEqual(['原神', '绝区零'])
  })

  it('② game_ids 为空 ⇒ 空列表（**不是**全量三款）', async () => {
    stubAuthAndGames([])
    await expect(fetchGames()).resolves.toEqual([])
  })

  it('③ fetchAllGames() 不按 game_ids 过滤 ⇒ 用户只持 1 款时仍回全量（游戏维护页 / 账号授权多选的防死锁钉子）', async () => {
    // 桩里 `/auth/me` 只给 [1]：若 `fetchAllGames` 也走 `game_ids` 过滤，这里只会拿到 [1]。
    stubAuthAndGames([1])
    const games = await fetchAllGames()
    expect(games.map((g) => g.id)).toEqual([1, 2, 3])
  })
})
