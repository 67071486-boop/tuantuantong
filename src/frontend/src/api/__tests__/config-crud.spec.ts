import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import { createConfig, listConfig, removeConfig, updateConfig, type ConfigRow } from '../config'

const mockedRequest = vi.mocked(request)

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue({})
})

const GAME: ConfigRow = {
  id: 1,
  name: '原神',
  sort_order: 1,
  is_active: true,
  created_at: '2026-09-20T10:00:00+08:00',
}

describe('listConfig：整页返回，父级与分页都进 params', () => {
  it('games：带分页参数，原样返回整页（不是 items）', async () => {
    const page = { items: [GAME], total: 1, page: 2, page_size: 50 }
    mockedRequest.mockResolvedValue(page)

    const result = await listConfig<ConfigRow>('games', { page: 2, page_size: 50 })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/games/',
      params: { page: 2, page_size: 50 },
    })
 // 返回**整页**而不是 items：列表页要用 total 渲染分页控件。
    expect(result).toBe(page)
  })

  it('categories：父级 game 进 params，资源名不被写错', async () => {
    await listConfig('categories', { game: 7, page: 1, page_size: 20 })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/categories/',
      params: { game: 7, page: 1, page_size: 20 },
    })
  })

  it('值为 undefined 的父级不进 params——直接断言键集合，别让 toEqual 的宽松语义蒙过去', async () => {
 // ⚠️ 这里**不能**写成 `expect(...).toHaveBeenCalledWith({params: {page: 1}})` 就完事：
 // vitest 的 `toEqual` 系断言**忽略值为 undefined 的属性**，于是「过滤掉 game」与
 // 「带着 game: undefined 发出去」在那种写法下**都会绿**（恒真用例）。
 // 键集合才是判据：删掉 `toListParams` 的过滤，这里的键会多出 `game` → 红。
    await listConfig('categories', { game: undefined, page: 1, page_size: 20 })

    expect(mockedRequest).toHaveBeenCalledTimes(1)
    const firstCall = mockedRequest.mock.calls[0]
    expect(firstCall, '没有发出请求').toBeDefined()
    const config = firstCall![0] as { params: Record<string, unknown> }
    expect(Object.keys(config.params)).toEqual(['page', 'page_size'])
  })
})

describe('写出口：路径与载荷', () => {
  it('createConfig → POST 集合路径，载荷原样', async () => {
    await createConfig('games', { name: '原神' })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/business/games/',
      data: { name: '原神' },
    })
  })

  it('updateConfig → PATCH 详情路径，只带改动字段', async () => {
    await updateConfig('categories', 9, { sort_order: 5 })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'patch',
      url: '/api/business/categories/9/',
      data: { sort_order: 5 },
    })
  })

  it('removeConfig → DELETE 详情路径', async () => {
    await removeConfig('projects', 11)

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'delete',
      url: '/api/business/projects/11/',
    })
  })
})
