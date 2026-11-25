import { describe, expect, it, vi } from 'vitest'

const request = vi.hoisted(() => vi.fn())
vi.mock('@/api/http', () => ({ request }))
import { fetchServiceDashboard } from '../serviceDashboard'

describe('客服工作台数据', () => {
  it('请求当前登录人的看板，不传入账号或租户标识', async () => {
    request.mockResolvedValueOnce({ today_created: 2 })
    await expect(fetchServiceDashboard()).resolves.toEqual({ today_created: 2 })
    expect(request).toHaveBeenCalledWith({ method: 'get', url: '/api/business/dashboard/service/' })
  })
})
