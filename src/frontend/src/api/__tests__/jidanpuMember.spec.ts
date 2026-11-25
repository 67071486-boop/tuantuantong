import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 「我的资料 → 功能设置」里那块**代练员个人绑定**的四个出口（下游鸡蛋铺线 Task 9；spec §9.2 / §9.3）。判据：**动词 + 路径（含尾斜杠）+ 请求体**。⚠️ 这四条路径与**租户级**那条（`jidanpu-binding/`）**刻意不同名**——它们多一段 `member/`，且「立即同步」是一条独立路径（⛔ 不挂在 `member/jidanpu-binding/` 下面：那条只收 GET/PUT）。路径写错时后端 404，页面只会显示一句空结果，所以钉在契约这一层。⚠️ **凭据红线**（spec §8.5 / §8-S1）：密码只在 PUT 的**请求体**里，⛔ 不进 URL、不进 `params`；「测试连接」「立即同步」两条**不许**带任何字段——它们的行主只来自登录态（越权面 = 0）。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  fetchMemberJidanpuBinding,
  saveMemberJidanpuBinding,
  syncMemberJidanpuNow,
  testMemberJidanpuConnection,
  type JidanpuMemberBindingPayload,
} from '../jidanpuMember'

const mockedRequest = vi.mocked(request)
const BINDING_URL = '/api/business/member/jidanpu-binding/'
const TEST_URL = '/api/business/member/jidanpu-binding/test-connection/'
const SYNC_NOW_URL = '/api/business/member/jidanpu-sync-now/'

/** 合成夹具（PII 守卫扫 `src/**`）：只仿形状，⛔ 不是真值。 */
const PAYLOAD: JidanpuMemberBindingPayload = {
  jidanpu_base_url: 'https://jidanpu-a.test.invalid',
  account: 'member-a',
  password: '',
  source_name: '团团通系统',
  throw_price_percent: 80,
  // ⭐ D-0299：「同步大厅订单」（默认开；页面两支载荷都必带这一格）。
  hall_sync_enabled: true,
  enabled: true,
}

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue(null)
})

describe('个人绑定（单行资源；只作用登录人自己那一行）', () => {
  it('读：GET 单数路径，未绑定时原样返回 null（**不**编一个空行）', async () => {
    mockedRequest.mockResolvedValue(null)
    await expect(fetchMemberJidanpuBinding()).resolves.toBeNull()
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'get', url: BINDING_URL })
  })

  it('存：**PUT**（不是 PATCH、不是 POST）', async () => {
    await saveMemberJidanpuBinding(PAYLOAD)
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'put', url: BINDING_URL, data: PAYLOAD })
    expect(mockedRequest.mock.calls[0]![0].method).not.toBe('patch')
  })

  it('存的载荷**恰好六个键**（密码空串照传 = 「不改」，后端契约）', async () => {
    await saveMemberJidanpuBinding(PAYLOAD)
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    // 写死字面量（E-0024）：⛔ 不拿 `Object.keys(PAYLOAD)` 当期望值——那与被测值同源，加一个键不会红。
    expect(Object.keys(config.data).sort()).toEqual([
      'account', 'enabled', 'hall_sync_enabled', 'jidanpu_base_url', 'password',
      'source_name', 'throw_price_percent',
    ])
    expect(config.data.password).toBe('')
    // 百分比是**数字**（后端那一列是 `PositiveSmallIntegerField`；字符串会在校验层变成一句
    // 「请填写合法的整数值」这种没人看得懂的报错）。
    expect(config.data.throw_price_percent).toBe(80)
  })

  it('凭据只进请求体：URL 与 params 里一个字符都没有（spec §8.5 红线）', async () => {
    await saveMemberJidanpuBinding({ ...PAYLOAD, password: 'pw-a' })
    const config = mockedRequest.mock.calls[0]![0] as Record<string, unknown>
    expect(Object.keys(config).sort()).toEqual(['data', 'method', 'url'])
    expect(String(config.url)).not.toContain('pw-a')
    expect(config.params).toBeUndefined()
    expect(JSON.stringify(config.url)).not.toContain('member-a')
  })

  /** ⭐ **②式（官方开放平台 API）那一支的载荷形状**。与①式那一条**并列**、⛔ 不合并：①式恰好六个键（`password` 在、`mode` 不在），②式是八个键（`mode` / `client_id` / `client_secret` 在，**`password` 一个字都不许有**）。⚠️ 两条都写死**字面量**键集（E-0024）：⛔ 不拿 `Object.keys(PAYLOAD)` 当期望值。 */
  it('存的载荷（②式）**恰好八个键**：`mode` / `client_id` / `client_secret` 在，⛔ 没有 `password`', async () => {
    const sentinel = 'sentinel-secret-member-do-not-render'
    // ⚠️ **`password` 必须整个键都不在**（⛔ 不是传空串）：②式那一路不登录，
    //    而「空串」在后端是「不改」⇒ 传它会留着①式那一格的旧密码（本用例钉的就是这件事）。
    // ⚠️ 类型上也显式收成 `JidanpuMemberBindingPayload`（两个键都是可选的）：⛔ 不靠
    //    `as` 把类型按下去——那样「页面真的多传了一个键」在类型层就看不见了。
    const openapi: JidanpuMemberBindingPayload = { ...PAYLOAD }
    delete openapi.password
    expect('password' in openapi).toBe(false)
    await saveMemberJidanpuBinding({
      ...openapi,
      mode: 'openapi',
      client_id: 'cid-synthetic-0001',
      client_secret: sentinel,
    })
    const config = mockedRequest.mock.calls[0]![0] as {
      data: Record<string, unknown>
      url: string
      params?: unknown
    }
    expect(Object.keys(config.data).sort()).toEqual([
      'account', 'client_id', 'client_secret', 'enabled', 'hall_sync_enabled',
      'jidanpu_base_url', 'mode', 'source_name', 'throw_price_percent',
    ])
    expect('password' in config.data).toBe(false)
    expect(config.data.mode).toBe('openapi')
    expect(config.data.client_id).toBe('cid-synthetic-0001')
    // ⛔ 密钥同一条红线：只在**请求体**里，⛔ 不进 URL、不进 `params`。
    expect(config.url).toBe(BINDING_URL)
    expect(String(config.url)).not.toContain(sentinel)
    expect(JSON.stringify(config)).not.toContain('cid-synthetic-0001/?')
    expect(config.params).toBeUndefined()
  })

  it('②式留空密钥 ⇒ 空串照传（空串 = 不改，后端契约）', async () => {
    await saveMemberJidanpuBinding({
      ...PAYLOAD, mode: 'openapi', client_id: 'cid-synthetic-0002', client_secret: '',
    })
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect(config.data.client_secret).toBe('')
  })

  it('②→① 切回来：载荷带 `mode: "password"`（⛔ 不是「不带 mode」）', async () => {
    await saveMemberJidanpuBinding({ ...PAYLOAD, mode: 'password' })
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect(config.data.mode).toBe('password')
    // ①式那三个键一个不少（这一跳是「补一个 mode」，⛔ 不是换一套载荷）。
    expect(Object.keys(config.data).sort()).toEqual([
      'account', 'enabled', 'hall_sync_enabled', 'jidanpu_base_url', 'mode',
      'password', 'source_name', 'throw_price_percent',
    ])
  })

  it('测试连接：POST 子路径、**空体**（凭据只从后端那一行取，⛔ 不把它做成跳板）', async () => {
    mockedRequest.mockResolvedValue({ ok: true, messages: [] })
    await testMemberJidanpuConnection()
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'post', url: TEST_URL })
  })

  /** ⭐（D-0177）：密码框此刻填的值 + 下拉里此刻选中的来源名一起带上。
   * ⚠️ 这条端点**原先**「连请求体都不读」（原句按 `AGENTS.md` §4 保留）⇒ 本用例是那件事
   * 改变后的**契约锚点**：两格都进 body，而**地址与账号仍然不进**。 */
  it('测试连接：密码 + 选中的来源名一起带上（D-0177），⛔ 地址账号仍不传', async () => {
    mockedRequest.mockResolvedValue({ ok: true, messages: [] })
    await testMemberJidanpuConnection('测试刚换的密码', '测试来源名')
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: TEST_URL,
      data: { password: '测试刚换的密码', source_name: '测试来源名' },
    })
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    // ⛔ 键集**恰好**这两个。
    expect(Object.keys(config.data).sort()).toEqual(['password', 'source_name'])
  })

  it('测试连接：两格都空 ⇒ 仍是**空体**（⛔ 不塞空串；空串在后端是「回落库里那一行」）', async () => {
    mockedRequest.mockResolvedValue({ ok: true, messages: [] })
    await testMemberJidanpuConnection('', '')
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'post', url: TEST_URL })
  })

  it('立即同步：POST 独立路径（⛔ 不挂在绑定那条路径下）、**空体**、结果原样返回 `applied`', async () => {
    mockedRequest.mockResolvedValue({ applied: 3 })
    await expect(syncMemberJidanpuNow()).resolves.toEqual({ applied: 3 })
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'post', url: SYNC_NOW_URL })
  })
})
