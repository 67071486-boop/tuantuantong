import { beforeEach, describe, expect, it, vi } from 'vitest'

/** 交易平台对接设置**七个出口**的传输契约（计划 T2/T3/T4；F2 追加 summary）。为什么需要它：页面 spec（`layui-island/pages/__tests__/system-tripartiteplatformsetting.spec.ts`）把整个 `@/api/platformIntegrations` 模块 `vi.mock` 掉了 ⇒ method / url / 请求体写错在页面测试里**全绿**。这条 spec 只测最薄的一层：`request()` 收到的配置原样是什么。形状照 `api/__tests__/shopBindings.spec.ts` / `orderSources.spec.ts`。凭据红线：`app_secret` / `pay_password` 是**只写**（后端响应不回），本文件只用`synthetic-*` 形状的合成值，断言它们**原样进请求体**（不是被前端过滤/改名）。 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import {
  createPlatformIntegration,
  deletePlatformIntegration,
  fetchPlatformIntegrationSummary,
  fetchPlatformIntegrations,
  fetchPlatformPushSettings,
  savePlatformPushSettings,
  updatePlatformIntegration,
} from '../platformIntegrations'

const mockedRequest = vi.mocked(request)
const URL = '/api/business/platform-integrations/'
const SETTINGS_URL = '/api/business/platform-integrations/settings/'
const SUMMARY_URL = '/api/business/platform-integrations/summary/'

beforeEach(() => {
  mockedRequest.mockReset()
  mockedRequest.mockResolvedValue(null)
})

describe('fetchPlatformIntegrations：列表（后端直接回数组）', () => {
  it('GET 集合路径，带回解包后的数组（不是 {items,total}）', async () => {
    const rows = [{ id: 1 }]
    mockedRequest.mockResolvedValue(rows)

    await expect(fetchPlatformIntegrations()).resolves.toBe(rows)
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'get', url: URL })
  })

  it('列表路径不是 settings 路径（防复制粘贴串端点）', async () => {
    await fetchPlatformIntegrations()
    const url = (mockedRequest.mock.calls[0]![0] as { url: string }).url
    expect(url).toBe(URL)
    expect(url).not.toBe(SETTINGS_URL)
  })
})

describe('写出口：方法与 URL', () => {
  it('新增 → POST 集合路径，载荷原样进 `data`', async () => {
    const payload = {
      platform: 10,
      account: 'acct-a',
      app_id: 'app-id-a',
      app_secret: 'synthetic-secret-a',
      pay_password: 'synthetic-pay-a',
      remark: '备注甲',
      contact_qq: 'qq-a',
      contact_phone: 'line-a',
    }
    await createPlatformIntegration(payload)

    expect(mockedRequest).toHaveBeenCalledWith({ method: 'post', url: URL, data: payload })
  })

  it('修改 → **PATCH** 详情路径（后端只开 GET/POST/PATCH/DELETE，没有 PUT）', async () => {
    await updatePlatformIntegration(7, { is_default: true })

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'patch',
      url: `${URL}7/`,
      data: { is_default: true },
    })
    expect(mockedRequest.mock.calls[0]![0].method).not.toBe('put')
  })

  it('删除 → DELETE 详情路径', async () => {
    await deletePlatformIntegration(7)

    expect(mockedRequest).toHaveBeenCalledWith({ method: 'delete', url: `${URL}7/` })
  })
})

describe('9 组全局设置：读 / 写', () => {
  it('读 → GET `…/settings/`（单例路径）', async () => {
    mockedRequest.mockResolvedValue({ quick_push_enabled: true })

    await expect(fetchPlatformPushSettings()).resolves.toEqual({ quick_push_enabled: true })
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'get', url: SETTINGS_URL })
  })

  it('写 → **PUT** `…/settings/`，9 个键原样进 `data`（布尔 / 整数都不改名）', async () => {
    const payload = {
      quick_push_enabled: false,
      legacy_simple_mode: 1,
      check_game_region: 0,
      forbid_push_when_double_gold_zero: true,
      duplicate_push_check: 1,
      select_jidanpu_center_by_default: true,
      auto_zero_refund_on_conflict: false,
      block_push_completed_orders: 1,
      auto_send_group_qr_after_accept: 1,
    }
    await savePlatformPushSettings(payload)

    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'put',
      url: SETTINGS_URL,
      data: payload,
    })
    const data = (mockedRequest.mock.calls[0]![0] as { data: Record<string, unknown> }).data
    expect(typeof data.legacy_simple_mode).toBe('number')
    expect(typeof data.quick_push_enabled).toBe('boolean')
  })
})

describe('F2：右面板摘要（规划 §4 的接口，后端 B6）', () => {
  it('GET `…/platform-integrations/summary/`，路径不是集合也不是 settings', async () => {
    const summary = {
      platform: 10,
      platform_label: '代练通',
      account: 'acct-a',
      configured: true,
      available_balance: '100.00',
      frozen_balance: '0.00',
      error: '',
    }
    mockedRequest.mockResolvedValue(summary)

    await expect(fetchPlatformIntegrationSummary()).resolves.toBe(summary)
    expect(mockedRequest).toHaveBeenCalledWith({ method: 'get', url: SUMMARY_URL })
    expect(SUMMARY_URL).not.toBe(URL)
    expect(SUMMARY_URL).not.toBe(SETTINGS_URL)
  })
})
