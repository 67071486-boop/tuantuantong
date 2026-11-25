import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import { fetchCsrf, fetchMe, login, logout } from '../auth'

const mocked = vi.mocked(request)

beforeEach(() => {
  mocked.mockReset()
})

describe('api/auth', () => {
  it('fetchCsrf 是 GET，且不触发 401 处理器', async () => {
    mocked.mockResolvedValue(null)
    await fetchCsrf()
    expect(mocked).toHaveBeenCalledWith({
      url: '/api/auth/csrf',
      method: 'GET',
      skipUnauthorizedHandler: true,
    })
  })

  it('login 用 POST 传用户名密码，并解出 data.user', async () => {
    mocked.mockResolvedValue({ user: { id: 1, username: 'kefu' } })
    const user = await login({ username: 'kefu', password: 'pw' })
    expect(mocked).toHaveBeenCalledWith({
      url: '/api/auth/login',
      method: 'POST',
      data: { username: 'kefu', password: 'pw' },
      skipUnauthorizedHandler: true,
    })
    expect(user).toEqual({ id: 1, username: 'kefu' })
  })

  it('logout 用 POST，且**不**跳过 401 处理器（它不是那三个例外之一）', async () => {
    mocked.mockResolvedValue(null)
    await logout()
    expect(mocked).toHaveBeenCalledWith({ url: '/api/auth/logout', method: 'POST' })
  })

  it('fetchMe 解出 data.user', async () => {
    mocked.mockResolvedValue({ user: { id: 2, username: 'admin' } })
    const user = await fetchMe()
    expect(mocked).toHaveBeenCalledWith({
      url: '/api/auth/me',
      method: 'GET',
      skipUnauthorizedHandler: true,
    })
    expect(user).toEqual({ id: 2, username: 'admin' })
  })
})
