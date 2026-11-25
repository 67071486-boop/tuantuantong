import { fetchMe } from './auth'
import { fetchAllGames, fetchGames, fetchServers } from './config'
import { AuthExpiredError, HttpError } from './errors'
import { request } from './http'
import { fetchOrderSources } from './orderSources'

type DictionaryOption = { id: number; name: string }

export type OrderDictionaries = {
  games: DictionaryOption[]
  servers: (DictionaryOption & { game: number })[]
  order_sources: DictionaryOption[]
}

export async function fetchOrderDictionaries({ scopedGames = false } = {}): Promise<OrderDictionaries> {
  try {
    const options = await request<OrderDictionaries>({
      method: 'get',
      url: '/api/business/orders/dictionary-options/',
    })
    if (!Array.isArray(options.games) || !Array.isArray(options.servers) || !Array.isArray(options.order_sources)) {
      throw new Error('订单筛选选项响应格式错误')
    }
    if (!scopedGames) return options
    const allowed = new Set((await fetchMe()).game_ids ?? [])
    return { ...options, games: options.games.filter(game => allowed.has(game.id)) }
  } catch (error) {
    if (error instanceof AuthExpiredError || (error instanceof HttpError && error.status === 403)) throw error
    // During a rolling deployment the Web dictionaries remain available to older backends.
    const gameReader = scopedGames ? fetchGames : fetchAllGames
    const [games, servers, order_sources] = await Promise.all([
      gameReader().catch(() => []),
      fetchServers().catch(() => []),
      fetchOrderSources().then(page => page?.items ?? []).catch(() => []),
    ])
    return { games, servers, order_sources }
  }
}
