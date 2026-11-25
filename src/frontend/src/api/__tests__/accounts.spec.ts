import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../http', () => ({ request: vi.fn() }))

import { request } from '../http'
import {
  createAccount,
  fetchAccountRoles,
  getAccount,
  listAccounts,
  resetAccountPassword,
  setAccountsStatus,
  setAccountStatus,
  updateAccount,
  type AccountWritePayload,
} from '../accounts'

const mocked = vi.mocked(request)

beforeEach(() => {
  mocked.mockReset()
  mocked.mockResolvedValue(undefined as never)
})

/** 这些断言打在**请求契约**上（URL / 方法 / 查询参数 / 载荷），不是打在返回值上——契约来自 `src/apps/accounts/` 的实现（spec §5 的端点表），返回值的解包由 `http.ts` 的既有测试覆盖。少了这一组，`/api/accounts/` 的路径写错、方法写成 `put`、或把 `is_active` 塞进写载荷里都不会被发现。 */
describe('账号接口的请求契约', () => {
  it('列表：GET /api/accounts/ + 查询参数', async () => {
    await listAccounts({ page: 2, page_size: 50, role: '客服', is_active: false, key: 'abc' })
    expect(mocked).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/accounts/',
      params: { page: 2, page_size: 50, role: '客服', is_active: false, key: 'abc' },
    })
  })

  it('列表：不传筛选时 params 为空对象（不凭空带 role/is_active）', async () => {
    await listAccounts()
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/accounts/', params: {} })
  })

  it('详情：GET /api/accounts/<id>/', async () => {
    await getAccount(7)
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/accounts/7/' })
  })

  it('职位下拉：GET /api/accounts/roles/', async () => {
    await fetchAccountRoles()
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/accounts/roles/' })
  })

  it('新增：POST /api/accounts/，载荷原样带出（不含服务端管理字段）', async () => {
    const payload: AccountWritePayload = {
      username: 'kefu01',
      display_name: '客服一号',
      role: '客服',
      games: [1, 2],
      commission_value: 10,
      grab_limit_dl: 0,
      grab_limit_pw: 0,
      hired_on: '2026-09-20',
      deposit_amount: 0,
    }
    await createAccount(payload)
    expect(mocked).toHaveBeenCalledWith({ method: 'post', url: '/api/accounts/', data: payload })
    // 「服务端管理字段」不得出现在载荷里（后端收到就拒 1000，不是静默忽略）。
    const sent = mocked.mock.calls[0]![0].data as Record<string, unknown>
    for (const key of ['password', 'is_active', 'is_superuser', 'is_staff']) {
      expect(sent).not.toHaveProperty(key)
    }
  })

  it('编辑：PATCH /api/accounts/<id>/（不是 PUT——部分更新语义）', async () => {
    await updateAccount(9, { display_name: '改名' })
    expect(mocked).toHaveBeenCalledWith({
      method: 'patch',
      url: '/api/accounts/9/',
      data: { display_name: '改名' },
    })
  })

  it('单项启停：POST /api/accounts/<id>/status/ + {is_active}（幂等 set 的目标值）', async () => {
    await setAccountStatus(3, false)
    expect(mocked).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/accounts/3/status/',
      data: { is_active: false },
    })
  })

  it('批量启停：POST /api/accounts/status/ + {ids,is_active}', async () => {
    await setAccountsStatus([1, 2, 3], true)
    expect(mocked).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/accounts/status/',
      data: { ids: [1, 2, 3], is_active: true },
    })
  })

  it('重置密码：POST /api/accounts/<id>/reset-password/（无请求体）', async () => {
    await resetAccountPassword(4)
    expect(mocked).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/accounts/4/reset-password/',
    })
  })

  /** ⭐ **T5：`business_type` 是筛选参数**（spec §7.1 C16 代价 ③）。
   * 「我们这一档 `0` 代练+陪玩」由**页面**决定不送（人对不上那个语义）⇒ API 层只负责原样透传，
   * 不在这里替调用方丢掉任何取值。 */
  it('listAccounts 把 business_type 原样送给后端（我们的 0 档由页面决定不送）', async () => {
    await listAccounts({ role: '代练员', business_type: 1, key: '三角洲', page_size: 200 })
    expect(mocked).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/accounts/',
      params: { role: '代练员', business_type: 1, key: '三角洲', page_size: 200 },
    })
  })
})
