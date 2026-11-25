import { afterEach, describe, expect, it } from 'vitest'
import { AxiosError, type AxiosAdapter, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { BizError, HttpError } from '../errors'
import { http } from '../http'
import { fetchMyOrderDetail, type MyOrderDetail } from '../myOrders'

/** `fetchMyOrderDetail` 的传输层测试（booster-h5 任务书 T4）。**真** `request()`（不 mock `@/api/http`）：只把 axios 的适配器换成假响应——手法与判据逐字沿用 `hall-transport.spec.ts`（同一个桩边界，打的是 `api/http.ts`的统一分支，不是我们的封装）。detail 是 GET（安全方法）⇒ 不碰 `document.cookie`，node 环境可直接跑、无需 csrf-cookie 的 mock（hall-transport 需要它是因为 POST）。 */

/** 合成单号（E-0077：全零段形状）。 */
const NO = '10000000000000000002'

/** §6.2 的 16 键 + **D-0301加的 `source_order_no`** = **恰好 17 键**合成载荷
 * （值也合成；`temp_amount` 字符串、两个可空键都给非空版）。 */
function detailFixture(): MyOrderDetail {
  return {
    order_no: NO,
    source_order_no: '',
    status: 'IN_PROGRESS',
    game_name: '测试游戏甲',
    server_name: '测试区服甲',
    project_text: '测试项目甲',
    content: '合成客户原始需求：十二层满星后截图',
    demand: '合成需求描述：打手看的这一段',
    time_requirement: '48小时',
    game_account: 'synth-account-a',
    game_sub_account: 'synth-sub-account-a',
    game_role_name: 'synth-role-a',
    is_urgent: true,
    remark: '合成重要备注：晚班交接',
    claimed_at: '2026-10-10T08:05:00+08:00',
    temp_amount: '88.00',
    group_qr_url: 'https://qr-synth.test.invalid/signed',
  }
}

function stubAdapter(status: number, data: unknown): AxiosAdapter {
  return async (config: InternalAxiosRequestConfig) => {
    const response: AxiosResponse = { data, status, statusText: '', headers: {}, config }
    if (status >= 200 && status < 300) return response
    throw new AxiosError(
      `Request failed with status code ${status}`,
      status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST,
      config,
      null,
      response,
    )
  }
}

/** 取出 promise 抛出的错误（不把断言写进 catch：`vitest/no-conditional-expect`）。 */
async function captureRejected(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise
  } catch (e) {
    return e
  }
  throw new Error('不应该走到这里：预期抛错但没有')
}

afterEach(() => {
  http.defaults.adapter = undefined
})

describe('fetchMyOrderDetail：GET /api/business/my-orders/<order_no>/detail/', () => {
  it('URL 形状（带尾斜杠）+ GET、不带 params / data', async () => {
    const calls: InternalAxiosRequestConfig[] = []
    http.defaults.adapter = (async (config: InternalAxiosRequestConfig) => {
      calls.push(config)
      return {
        data: { code: 0, msg: '', data: detailFixture() },
        status: 200,
        statusText: '',
        headers: {},
        config,
      }
    }) as AxiosAdapter

    await fetchMyOrderDetail(NO)

    expect(calls).toHaveLength(1)
    const first = calls[0]
    if (first === undefined) throw new Error('spec: 请求不存在')
    expect(first.method).toBe('get')
    expect(first.url).toBe(`/api/business/my-orders/${NO}/detail/`)
    expect(first.params).toBeUndefined()
    expect('data' in first ? first.data : undefined).toBeUndefined()
  })

  it('200 + code 0 → 解包出 data：16 键原样透传（本层不挑键、不改名）', async () => {
    const payload = detailFixture()
    http.defaults.adapter = stubAdapter(200, { code: 0, msg: '', data: payload })
    // `toEqual` 逐键比对：哪天本层裁掉 / 改名任何一个键，这条就红。
    await expect(fetchMyOrderDetail(NO)).resolves.toEqual(payload)
  })

  it('200 + 业务码 1002（单不存在 / 软删）→ BizError，code/msg 都来自后端信封', async () => {
    http.defaults.adapter = stubAdapter(200, {
      code: 1002,
      msg: '记录不存在或已被删除',
      data: null,
    })
    const err = await captureRejected(fetchMyOrderDetail(NO))
    expect(err).toBeInstanceOf(BizError)
    expect((err as BizError).code).toBe(1002)
    expect((err as BizError).message).toBe('记录不存在或已被删除')
  })

  it('HTTP 403（非接单人 / 无节点）→ HttpError(403)，msg 取自信封原样', async () => {
    http.defaults.adapter = stubAdapter(403, {
      code: 403,
      msg: '该单不是你接的，不能查看详情',
      data: null,
    })
    const err = await captureRejected(fetchMyOrderDetail(NO))
    expect(err).toBeInstanceOf(HttpError)
    expect((err as HttpError).status).toBe(403)
    expect((err as HttpError).message).toBe('该单不是你接的，不能查看详情')
  })
})
