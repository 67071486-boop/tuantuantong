import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  TAKER_DEPOSIT_QUOTA_NODE,
  depositQuotaUrl,
  fetchDepositQuota,
  saveDepositQuota,
  setDepositQuotaEnabled,
  type DepositQuota,
  type DepositQuotaPayload,
} from '../takerDepositQuota'

/** 打手个人押金额度接口层（`api/takerDepositQuota.ts`）的**请求形状**测试。钉的是真实出口：**方法 + URL + 载荷**。契约出处 = 计划`docs/plans/2026-10-23-taker-deposit-quota-plan.md` §5.1：- 读：`GET /api/business/takers/<user_id>/deposit-quota/`；- 写：**PUT** 同一路径（不是 POST/PATCH），载荷 `{enabled, amounts}`；- `amounts` 的键是 **game_id 的字符串**（后端收 `dict[str, str]`）。⚠️ 夹具一律**合成值**（E-0077）。 */
const mockedRequest = vi.mocked(request)

type Call = { method?: string; url?: string; data?: unknown }
const calls: Call[] = []

/** 一份合成回包（每行取值互不相同，便于分辨拿错行）。 */
function quotaFixture(): DepositQuota {
  return {
    user_id: 12,
    username: 'sq001',
    display_name: '张三',
    enabled: true,
    games: [
      { game_id: 1, game_name: '原神', global_amount: '50.00', amount: null },
      { game_id: 2, game_name: '三角洲', global_amount: '300.00', amount: '300.00' },
    ],
  }
}

beforeEach(() => {
  calls.length = 0
  mockedRequest.mockReset()
  mockedRequest.mockImplementation(async (config: Call) => {
    calls.push(config)
    return quotaFixture()
  })
})

describe('路径与权限节点', () => {
  it('URL 逐字：`/api/business/takers/<user_id>/deposit-quota/`（尾斜杠不能少）', () => {
    expect(depositQuotaUrl(12)).toBe('/api/business/takers/12/deposit-quota/')
    // 节点的唯一出处就在本模块（岛页只 import 常量，不写带点字面量——见模块注释第 3 条）。
    expect(TAKER_DEPOSIT_QUOTA_NODE).toBe('finance.taker_wallet_adjust')
  })
})

describe('读 / 写', () => {
  it('GET：方法 / URL / 不带参数；回包原样返回', async () => {
    const result = await fetchDepositQuota(12)
    expect(calls).toEqual([{ method: 'get', url: '/api/business/takers/12/deposit-quota/' }])
    expect(result.user_id).toBe(12)
    expect(result.games[0]!.amount).toBeNull()
  })

  it('PUT：同一路径；载荷 `{enabled, amounts}`（键是 game_id 字符串、值原样转发）', async () => {
    const payload: DepositQuotaPayload = { enabled: false, amounts: { '1': '0', '2': '300.00' } }
    await saveDepositQuota(12, payload)
    expect(calls).toEqual([
      {
        method: 'put',
        url: '/api/business/takers/12/deposit-quota/',
        // ⛔ 不在接口层做类型转换：金额是字符串（Decimal 不经过 JS 浮点）。
        data: { enabled: false, amounts: { '1': '0', '2': '300.00' } },
      },
    ])
  })

  it('⭐ 行内开关只发 `{enabled}`：载荷里**没有** `amounts` 键（= 后端不动任何额度行）', async () => {
    await setDepositQuotaEnabled(12, true)
    const data = calls[0]!.data as Record<string, unknown>
    // 判据打在**真实出口**（`request` 的 data）上：
    expect(data).toEqual({ enabled: true })
    // ⛔ 不是「amounts 是空对象」——键**根本不在**。后者今天等价，但它会让下一个人
    //    以为「开关要连着额度一起发」，顺手改成当前值就变成一次开关写一遍额度（E-0027 同型）。
    expect(Object.prototype.hasOwnProperty.call(data, 'amounts')).toBe(false)
    // 端点 / 方法不变（仍是那个 PUT，不新增端点）。
    expect(calls[0]!.method).toBe('put')
    expect(calls[0]!.url).toBe('/api/business/takers/12/deposit-quota/')
  })
})
