import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../http', () => ({ request: vi.fn() }))

import { request } from '../http'
import {
  buildRealNameLogParams,
  buyButtonLabel,
  getRealNameQuota,
  getRealNameSetting,
  listRealNameLogs,
  REALNAME_STATUS_ALL,
  REALNAME_STATUS_FAIL,
  REALNAME_STATUS_PASS,
  updateRealNameSetting,
  verifyRealName,
  type RealNameLogParams,
} from '../realname'

const mocked = vi.mocked(request)

beforeEach(() => {
  mocked.mockReset()
  mocked.mockResolvedValue(undefined as never)
})

/** 这一组断言打在**请求契约**上（URL / 方法 / 查询参数 / 载荷）——契约来自`src/apps/accounts/views.py` 的四个 `realname*` action 与 `serializers.py` 的两个序列化器，不是照 spec 的简写抄的。⭐ **两段式取数**（对标 `RnameCertLogAndSetting.js:27,35` vs `:151-154`）的**线上形状**只在这一组里能被看到：页面那一侧断言的是「交给取数层的 `where`」，把 `where` 变成查询串的是 `http.ts` 的 `params` ⇒ 少了这一组，「首屏到底发了什么」在**请求层**是零覆盖的。 */
describe('实名记录列表的请求契约（GET /api/accounts/realname-logs/）', () => {
  it('端点与分页参数名：`page` / `limit`（⛔ **不是**账号列表那页的 `page_size`）', async () => {
    await listRealNameLogs({ sdate: '2026-09-11', edate: '2026-10-10', page: 2 }, true)
    expect(mocked).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/accounts/realname-logs/',
      params: { sdate: '2026-09-11', edate: '2026-10-10', page: 2 },
    })
  })

  it('⭐ 两段式的第一段：`includeFilters: false` ⇒ 查询参数里**只有** `sdate`/`edate`', async () => {
    await listRealNameLogs(
      {
        sdate: '2026-09-11',
        edate: '2026-10-10',
        status: '1',
        key1: 'kefu_jia',
        key2: '测试姓名甲',
        key3: '110101',
      },
      false,
    )
    const params = mocked.mock.calls[0]![0].params as Record<string, unknown>
    expect(Object.keys(params).sort()).toEqual(['edate', 'sdate'])
    for (const key of ['status', 'key1', 'key2', 'key3']) {
      expect(params, `首屏不该带 ${key}`).not.toHaveProperty(key)
    }
  })

  it('⭐ 两段式的第二段：`includeFilters: true` ⇒ 四个筛选都进查询参数（`certStatus` 控件名落成 `status`）', async () => {
    await listRealNameLogs(
      {
        sdate: '2026-09-11',
        edate: '2026-10-10',
        status: '2',
        key1: 'kefu_jia',
        key2: '测试姓名甲',
        key3: '110101',
      },
      true,
    )
    const params = mocked.mock.calls[0]![0].params as Record<string, unknown>
    expect(params).toEqual({
      sdate: '2026-09-11',
      edate: '2026-10-10',
      status: '2',
      key1: 'kefu_jia',
      key2: '测试姓名甲',
      key3: '110101',
    })
    // ⛔ 参数名不是控件名。
    expect(params).not.toHaveProperty('certStatus')
    expect(params).not.toHaveProperty('cert_status')
  })

  it('`status` 是**字符枚举**且 `"0"`（全部）必须原样发出去（后端用 `is not None` 判传没传）', () => {
    expect([REALNAME_STATUS_ALL, REALNAME_STATUS_FAIL, REALNAME_STATUS_PASS]).toEqual([
      '0',
      '1',
      '2',
    ])
    const params: RealNameLogParams = { status: REALNAME_STATUS_ALL }
    expect(buildRealNameLogParams(params, true)).toEqual({ status: '0' })
    // 数值 `0` 也会被留下（`put` 的判据是「空」而不是真值）——但接口那一侧收的是字符串枚举，
    // 这里只钉「没有被真值判断丢掉」这一条。
    expect(buildRealNameLogParams({ page: 0 }, true)).toEqual({ page: 0 })
  })

  it('空串键一律不发（后端已把空串当「没选」，前端不再送一个空的进去）', () => {
    expect(
      buildRealNameLogParams({ sdate: '  ', edate: '2026-10-10', status: '', key1: '' }, true),
    ).toEqual({ edate: '2026-10-10' })
  })
})

describe('实名那四项配置的请求契约（GET / PATCH /api/accounts/realname-setting/）', () => {
  it('读：GET，无 body；写：**PATCH** 且载荷原样带出（`patch` 是部分更新 ⇒ 只发改的键）', async () => {
    await getRealNameSetting()
    expect(mocked).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/accounts/realname-setting/',
    })

    await updateRealNameSetting({ enabled: true, min_age: 18, max_age: 0 })
    expect(mocked).toHaveBeenLastCalledWith({
      method: 'patch',
      url: '/api/accounts/realname-setting/',
      data: { enabled: true, min_age: 18, max_age: 0 },
    })
  })

  it('`false` 与 `0` 是**合法写入值**，不许被当成「没传」丢掉（E-0021）', () => {
    const payload = { enabled: false, min_age: 0, max_age: 0 }
    expect(Object.keys(payload).sort()).toEqual(['enabled', 'max_age', 'min_age'])
    expect(payload.enabled).toBe(false)
  })
})

describe('余量的请求契约（GET /api/accounts/realname-quota/）与按钮文案', () => {
  it('端点：GET，无参数', async () => {
    await getRealNameQuota()
    expect(mocked).toHaveBeenCalledWith({ method: 'get', url: '/api/accounts/realname-quota/' })
  })

  it('按钮文案：括号是**半角**、数字取服务端读数（⛔ 不写死 0）——T3 起是「配置实名资源包」', () => {
    // T3（spec §2.4，业务方拍板改应用内配置）：文案从「购买实名认证套餐包」
    // 换成「配置实名资源包」，括号形状（半角）与 N 的取值链不动。
    expect(buyButtonLabel(37)).toBe('配置实名资源包(当前余量37次)')
    // `0` 是合法读数（没买过 / 买过用完），照实写 0。
    expect(buyButtonLabel(0)).toBe('配置实名资源包(当前余量0次)')
    // `null` = 跨 schema 读不到 ⇒ 按钮回落到字面量 0，
    // 而**必须**由页面另外给一句提示（`remaining === null` 那一支，判据在页测试里）。
    expect(buyButtonLabel(null)).toBe('配置实名资源包(当前余量0次)')
    expect(buyButtonLabel(37)).not.toContain('（')
  })
})

/** ⭐ 用户自助核验的请求契约（实名认证线 T10）。⚠️ **路径前缀与上面四组不同**：这个端点挂在 `apps/realname/urls.py`（`config/urls.py` 里 `path("api/auth/realname/", …)`），**不是** `api/accounts/…`⇒ 若有人"顺手"改成 `${BASE}…`，这条用例当场红（而页测试用的是替身，看不到 URL）。⚠️ 字段名是后端序列化器的 `name` / `id_card_no`，⛔ 不是页内控件名 `certName` / `certSid`。 */
describe('自助核验的请求契约（POST /api/auth/realname/verify/）', () => {
  it('端点、方法、载荷逐字：⛔ 不是 `api/accounts/…`，字段名也不是页内控件名', async () => {
    await verifyRealName({ name: '测试姓名甲', id_card_no: '11010119900307561X' })
    expect(mocked).toHaveBeenCalledWith({
      method: 'post',
      url: '/api/auth/realname/verify/',
      data: { name: '测试姓名甲', id_card_no: '11010119900307561X' },
    })
  })
})
