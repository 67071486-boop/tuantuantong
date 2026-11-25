import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 上家鸡蛋铺线 Task 12：「我的资料」那块**上家个人绑定**的四个出口 + 大厅第三路的接单出口（spec §9.1 / §9.2 / §9.3 / §6.1）。判据：**动词 + 路径（含尾斜杠）+ 请求体**。⚠️ 四条绑定路径与另两条鸡蛋铺线**刻意不同名**（本文件多一段 `upstream-`）：路径写错时后端 404，页面只会显示一句空结果 ⇒ 钉在契约这一层。⚠️ **凭据红线**（spec §8.3）：密码只在 PUT 的**请求体**里，⛔ 不进 URL、不进 `params`；「测试连接」「立即同步」两条**不许**带任何字段（行主只来自登录态，越权面 = 0）。⚠️ **接单端点与另两路都不同**（`hall/upstream/<pk>/claim/`）：参数是**镜像行主键**，⛔ 不是上家单号、⛔ 更不是 P2 那条 `hall/remote/<来源>/<单号>/claim/`。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  claimUpstreamMirror,
  createUpstreamBindingRow,
  getUpstreamBinding,
  getUpstreamBindingRow,
  putUpstreamBinding,
  syncUpstreamNow,
  testUpstreamConnection,
  updateUpstreamBindingRow,
  type JidanpuUpstreamBindingPayload,
} from '../jidanpuUpstream'

const mockedRequest = vi.mocked(request)
const BINDING_URL = '/api/business/member/upstream-jidanpu-binding/'
const TEST_URL = '/api/business/member/upstream-jidanpu-binding/test-connection/'
const SYNC_NOW_URL = '/api/business/member/upstream-jidanpu-sync-now/'
const CLAIM_PREFIX = '/api/business/hall/upstream/'
/** 行级四条（POST 集合 + GET/PUT/DELETE 明细；E-0255 / D-0256）。 */
const ROW_BASE_URL = '/api/business/upstream-bindings/jidanpu/'

/** 合成夹具（PII 守卫扫 `src/**`）：只仿形状，⛔ 不是真值。 */
const PAYLOAD: JidanpuUpstreamBindingPayload = {
  jidanpu_base_url: 'https://upstream-a.test.invalid',
  account: 'kefu-up-a',
  password: '',
  hall_price_percent: 60,
  enabled: true,
}

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue(null)
})

describe('上家个人绑定（单行资源；只作用登录人自己那一行）', () => {
  it('读：GET 单数路径，未绑定时原样返回 null（**不**编一个空行）', async () => {
    mockedRequest.mockResolvedValue(null)
    await expect(getUpstreamBinding()).resolves.toBeNull()
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'get', url: BINDING_URL })
  })

  it('存：**PUT**（不是 PATCH、不是 POST）', async () => {
    await putUpstreamBinding(PAYLOAD)
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'put', url: BINDING_URL, data: PAYLOAD })
    expect(mockedRequest.mock.calls[0]![0].method).not.toBe('patch')
  })

  it('存的载荷**恰好五个键**（密码空串照传 = 「不改」，后端契约）', async () => {
    await putUpstreamBinding(PAYLOAD)
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    // 写死字面量（E-0024）：⛔ 不拿 `Object.keys(PAYLOAD)` 当期望值——那与被测值同源，加一个键不会红。
    expect(Object.keys(config.data).sort()).toEqual([
      'account', 'enabled', 'hall_price_percent', 'jidanpu_base_url', 'password',
    ])
    expect(config.data.password).toBe('')
    // 百分比是**数字**（后端那一列是 `PositiveSmallIntegerField`；字符串会在校验层变成一句
    // 「请填写合法的整数值」这种没人看得懂的报错）。
    expect(config.data.hall_price_percent).toBe(60)
    // ⛔ 两个轮询间隔**不在载荷里**（页面没有那两个输入框）——多带它们等于让页面上没有的东西
    //    偷偷改库里的配置。
    expect(config.data).not.toHaveProperty('hall_poll_seconds')
    expect(config.data).not.toHaveProperty('my_orders_poll_seconds')
  })

  it('凭据只进请求体：URL 与 params 里一个字符都没有（spec §8.3 红线）', async () => {
    await putUpstreamBinding({ ...PAYLOAD, password: 'pw-up-a' })
    const config = mockedRequest.mock.calls[0]![0] as Record<string, unknown>
    expect(Object.keys(config).sort()).toEqual(['data', 'method', 'url'])
    expect(String(config.url)).not.toContain('pw-up-a')
    expect(config.params).toBeUndefined()
    expect(JSON.stringify(config.url)).not.toContain('kefu-up-a')
  })

  it('测试连接：POST 子路径、**空体**（凭据只从后端那一行取，⛔ 不把它做成跳板）', async () => {
    mockedRequest.mockResolvedValue({ ok: true, messages: [] })
    await testUpstreamConnection()
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'post', url: TEST_URL })
  })

  it('立即同步：POST 独立路径（⛔ 不挂在绑定那条路径下）、**空体**、结果原样返回 `applied`', async () => {
    mockedRequest.mockResolvedValue({ applied: 3 })
    await expect(syncUpstreamNow()).resolves.toEqual({ applied: 3 })
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'post', url: SYNC_NOW_URL })
  })
})

describe('行级四条（E-0255 / D-0256：绑定是多行资源；新增 / 编辑 / 回填走这里）', () => {
  it('创建：**POST** 集合路径、载荷与 member PUT 同形（恰好五键）', async () => {
    await createUpstreamBindingRow(PAYLOAD)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: ROW_BASE_URL,
      data: PAYLOAD,
    })
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect(Object.keys(config.data).sort()).toEqual([
      'account', 'enabled', 'hall_price_percent', 'jidanpu_base_url', 'password',
    ])
  })

  it('⭐ 2026-10-29：`name`（上家系统名称，选填）原样随行级 POST 发出（空串 = 清除）', async () => {
    await createUpstreamBindingRow({ ...PAYLOAD, name: '初音网游' })
    const config = mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }
    expect(config.data.name).toBe('初音网游')
    // 空串也**照发**（不是省略键）：这一格可回显，空串就是「用户清空了名称」。
    await updateUpstreamBindingRow(23, { ...PAYLOAD, name: '' })
    const put = mockedRequest.mock.calls[1]![0] as { data: Record<string, unknown> }
    expect(put.data.name).toBe('')
  })

  it('编辑：**PUT** 明细路径（`<pk>/` 带尾斜杠），⛔ 不是 PATCH、⛔ 不是 member 那条单行资源', async () => {
    await updateUpstreamBindingRow(23, PAYLOAD)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'put',
      url: `${ROW_BASE_URL}23/`,
      data: PAYLOAD,
    })
    const config = mockedRequest.mock.calls[0]![0] as { url: string }
    expect(String(config.url)).not.toContain('member/upstream-jidanpu-binding')
  })

  it('回填：**GET** 明细路径（统一列表 11 键里没有 enabled / 百分比，只能按主键读一次）', async () => {
    await getUpstreamBindingRow(23)
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'get', url: `${ROW_BASE_URL}23/` })
  })

  it('凭据只进请求体：行级 GET / PUT 的 URL 与 params 里一个字符都没有（spec §8.3 红线）', async () => {
    await updateUpstreamBindingRow(23, { ...PAYLOAD, password: 'pw-up-row' })
    const put = mockedRequest.mock.calls[0]![0] as Record<string, unknown>
    expect(String(put.url)).not.toContain('pw-up-row')
    expect(put.params).toBeUndefined()
    await getUpstreamBindingRow(23)
    const get = mockedRequest.mock.calls[1]![0] as Record<string, unknown>
    expect(get).toEqual({ method: 'get', url: `${ROW_BASE_URL}23/` })
  })

  it('主键不是**正整数** ⇒ GET / PUT 一次请求都不发（形状先行拒绝，E-0021 同族）', async () => {
    for (const bad of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      await expect(getUpstreamBindingRow(bad)).rejects.toThrow()
      await expect(updateUpstreamBindingRow(bad, PAYLOAD)).rejects.toThrow()
    }
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})

describe('大厅第三路接单（参数是**镜像行主键**）', () => {
  it('打的是本线的 `<pk>/claim/` 端点，⛔ 不是 P2 那条 `hall/remote/<来源>/<单号>/claim/`', async () => {
    mockedRequest.mockResolvedValue({ order_no: '20260928000000000001' })
    await claimUpstreamMirror(7)
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'post',
      url: `${CLAIM_PREFIX}7/claim/`,
    })
    const url = String((mockedRequest.mock.calls[0]![0] as { url: string }).url)
    // 逐条钉：本线路的路径形状（来源与单号那两段**不该出现**）。
    expect(url).not.toContain('/hall/remote/')
    expect(url).not.toContain('/jidanpu/')
  })

  it('主键不是**正整数** ⇒ 一次请求都不发（形状先行拒绝，E-0021 同族）', async () => {
    for (const bad of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      await expect(claimUpstreamMirror(bad)).rejects.toThrow()
    }
    expect(mockedRequest).not.toHaveBeenCalled()
  })
})
