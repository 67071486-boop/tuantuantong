import { request } from './http'

export type GameLibraryRow = {
  id: number
  name: string
  first_key: string
  sort_order: number
}

/** 平台游戏库（租户库内一份）：新增游戏弹窗的候选清单。 */
export async function fetchGameLibrary(): Promise<GameLibraryRow[]> {
  const data = await request<{ items: GameLibraryRow[] }>({
    method: 'get',
    url: '/api/business/game-library/',
  })
  return data.items
}
