import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../http', () => ({ request: vi.fn() }))

import { request } from '../http'
import { changePassword, fetchMyProfile, patchMyProfile } from '../myProfile'

const mocked = vi.mocked(request)

beforeEach(() => mocked.mockReset())

describe('myProfile api', () => {
  it('读：GET /api/auth/profile 并解出 profile', async () => {
    mocked.mockResolvedValueOnce({ profile: { username: 'kefu01' } } as never)
    await expect(fetchMyProfile()).resolves.toEqual({ username: 'kefu01' })
    expect(mocked).toHaveBeenCalledWith({ url: '/api/auth/profile', method: 'GET' })
  })

  it('写：PATCH 只带传进来的键', async () => {
    mocked.mockResolvedValueOnce({ profile: { username: 'kefu01' } } as never)
    await patchMyProfile({ mobile: '13800000000' })
    expect(mocked).toHaveBeenCalledWith({
      url: '/api/auth/profile', method: 'PATCH', data: { mobile: '13800000000' },
    })
  })

  it('改密：POST /api/auth/password', async () => {
    mocked.mockResolvedValueOnce(null as never)
    await changePassword({ old_password: 'a', new_password: 'b', confirm_password: 'b' })
    expect(mocked).toHaveBeenCalledWith({
      url: '/api/auth/password', method: 'POST',
      data: { old_password: 'a', new_password: 'b', confirm_password: 'b' },
    })
  })
})
