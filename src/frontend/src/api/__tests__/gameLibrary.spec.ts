import { expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import { fetchGameLibrary } from '../gameLibrary'

it('从租户游戏库接口取候选清单', async () => {
  vi.mocked(request).mockResolvedValueOnce({
    items: [{ id: 1, name: '蓝色星原：旅谣', first_key: 'L', sort_order: 1 }],
  })
  const rows = await fetchGameLibrary()
  expect(request).toHaveBeenCalledWith({
    method: 'get',
    url: '/api/business/game-library/',
  })
  expect(rows).toEqual([{ id: 1, name: '蓝色星原：旅谣', first_key: 'L', sort_order: 1 }])
})
