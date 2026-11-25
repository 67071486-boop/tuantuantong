// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【APP与小程序 → 数据概览】island 页的**交互**测试。
 *
 * 计划：`docs/plans/2026-10-23-app-mp-stats.md` §7/§8。
 *
 * 钉住四件有真实后果的事：
 * 1. **微信块失败 / 未开通 ⇒ 只显示「暂不可用」+ 原因，⛔ 不画表、不写 0**（计划硬口径）；
 * 2. 首屏默认 **7 天**、点「近 30 天」真的按 30 重查并重绘；
 * 3. **权限门 fail-closed**：没节点 ⇒ 一句提示 + **不注入标记** + **不发业务请求**；
 * 4. 接单 / 注册 / 押金三块把后端给的数字原样上屏。
 *
 * ⚠️ 三条 island 陷阱（`AGENTS.md` §5-G）：类名 / 选择器分两个常量；以 `(` 开头的语句
 * 前置分号；顺序是 `bootstrapIsland` → 塞标记 → `initIslandElements`（本文件按运行期 DOM 判）。
 * ⚠️ 夹具一律合成值（E-0077）。
 */
const LAYUI = vi.hoisted(() => {
  const messages: string[] = []
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    messages,
    seenByBootstrap,
    seenByInit,
    layer: { msg: vi.fn((text: string) => messages.push(text)) },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#appStatsWechat') !== null)
    return { layer: LAYUI.layer }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#appStatsWechat') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => {
    LAYUI.messages.push(text)
  },
}))

vi.mock('@/api/http', () => ({ request: vi.fn() }))
vi.mock('@/api/auth', () => ({ fetchMe: vi.fn() }))

import { fetchMe } from '@/api/auth'
import { request } from '@/api/http'
import type { AppStatsOverview } from '@/api/appStats'
import page, {
  CLAIM_ID,
  DEPOSIT_ID,
  ERROR_ID,
  MESSAGES,
  NO_ACCESS_MESSAGE,
  PANEL_SELECTOR,
  RANGE_BUTTON_SELECTOR,
  REGISTRATION_ID,
  WECHAT_ID,
} from '../app-stats'

const mockedRequest = vi.mocked(request)
const mockedFetchMe = vi.mocked(fetchMe)

function me(permissions: string[] = ['finance.support_statistics']) {
  return {
    id: 1,
    username: 'stats-viewer',
    display_name: '合成管理员',
    identity_plane: 'tenant_a',
    is_tenant_admin: false,
    is_account_admin: false,
    roles: [] as string[],
    permissions: [] as string[],
    menu_permissions: permissions,
  }
}

function overview(over: Partial<AppStatsOverview> = {}): AppStatsOverview {
  return {
    days: 7,
    date_from: '2026-10-17',
    date_to: '2026-10-23',
    wechat: {
      available: true,
      reason: '',
      date_from: '2026-10-16',
      date_to: '2026-10-22',
      series: [
        { date: '2026-10-21', session_cnt: 5, visit_pv: 6, visit_uv: 7, visit_uv_new: 1 },
        { date: '2026-10-22', session_cnt: 8, visit_pv: 9, visit_uv: 10, visit_uv_new: 2 },
      ],
      totals: { session_cnt: 13, visit_pv: 15, visit_uv: 17, visit_uv_new: 3 },
    },
    registrations: {
      series: [
        { date: '2026-10-23', mp_weixin: 2, app_android: 1, unknown: 0, total: 3 },
      ],
      totals: { mp_weixin: 2, app_android: 1, unknown: 0, total: 3 },
      note: '历史未区分：上线前的账号...',
    },
    claims: {
      series: [
        {
          date: '2026-10-23',
          total: 4,
          mobile: 3,
          by_reason: { 打手App: 3, 系统网页: 1 },
        },
      ],
      totals: { total: 4, mobile: 3, by_reason: { 打手App: 3, 系统网页: 1 } },
      mobile_channels: ['打手App'],
    },
    deposit: {
      total_frozen: '120.00',
      taker_count: 2,
      series: [
        { date: '2026-10-22', delta: '20.00' },
        { date: '2026-10-23', delta: '-5.00' },
      ],
      note: '账面冻结额（含到期未结算）',
    },
    ...over,
  }
}

let root: HTMLElement

beforeEach(() => {
  root = document.createElement('div')
  document.body.appendChild(root)
  LAYUI.messages.length = 0
  LAYUI.seenByBootstrap.length = 0
  LAYUI.seenByInit.length = 0
  mockedRequest.mockReset()
  mockedFetchMe.mockReset()
  mockedFetchMe.mockResolvedValue(me())
})

afterEach(() => {
  root.remove()
})

async function mountPage(): Promise<void> {
  await page.mount(root, { onNavigate: () => undefined })
}

describe('APP与小程序 · 数据概览页', () => {
  it('首屏按 7 天取数，四块都上屏（押金口径照后端原样）', async () => {
    mockedRequest.mockResolvedValueOnce(overview())
    await mountPage()

    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(mockedRequest.mock.calls[0]?.[0]).toMatchObject({
      method: 'get',
      url: '/api/business/app-stats/overview/',
      params: { days: 7 },
    })
    expect(root.querySelector(PANEL_SELECTOR)).not.toBeNull()
    // 微信表真的画出来了（有访问人数 10）。
    const wechat = root.querySelector<HTMLElement>(`#${WECHAT_ID}`)
    expect(wechat?.querySelector('table')).not.toBeNull()
    expect(wechat?.textContent).toContain('10')
    // 注册 / 接单 / 押金三块的值。
    expect(root.querySelector<HTMLElement>(`#${REGISTRATION_ID}`)?.textContent).toContain('2')
    expect(root.querySelector<HTMLElement>(`#${CLAIM_ID}`)?.textContent).toContain('移动端 3 单')
    expect(root.querySelector<HTMLElement>(`#${DEPOSIT_ID}`)?.textContent).toContain('120.00')
    expect(root.querySelector<HTMLElement>(`#${DEPOSIT_ID}`)?.textContent).toContain(
      '账面冻结额（含到期未结算）',
    )
    // 陷阱 ③：标记是 `initIslandElements` 之后才该被看到的，`bootstrapIsland` 时还没有。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    expect(LAYUI.seenByInit).toEqual([true])
  })

  it('微信不可用 ⇒ 只显示「暂不可用」+ 原因，⛔ 不画表、不写 0', async () => {
    mockedRequest.mockResolvedValueOnce(
      overview({
        wechat: {
          available: false,
          reason: '本租户未开通小程序',
          date_from: '2026-10-16',
          date_to: '2026-10-22',
          series: [],
          totals: null,
        },
      }),
    )
    await mountPage()
    const wechat = root.querySelector<HTMLElement>(`#${WECHAT_ID}`)
    expect(wechat?.textContent).toContain(MESSAGES.unavailable)
    expect(wechat?.textContent).toContain('本租户未开通小程序')
    expect(wechat?.querySelector('table')).toBeNull()
    // 判别力：把「0」写上去的实现会让这条红。
    expect(wechat?.textContent).not.toContain('0 次')
  })

  it('点「近 30 天」真的按 30 重查并重绘', async () => {
    mockedRequest.mockResolvedValueOnce(overview())
    await mountPage()
    const thirty = root.querySelector<HTMLElement>(`${RANGE_BUTTON_SELECTOR}[data-days="30"]`)
    expect(thirty).not.toBeNull()

    mockedRequest.mockResolvedValueOnce(
      overview({
        days: 30,
        claims: {
          series: [{ date: '2026-10-23', total: 40, mobile: 30, by_reason: { 打手App: 30 } }],
          totals: { total: 40, mobile: 30, by_reason: { 打手App: 30 } },
          mobile_channels: ['打手App'],
        },
      }),
    )
    thirty?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenCalledTimes(2)
    })
    expect(mockedRequest.mock.calls[1]?.[0]).toMatchObject({ params: { days: 30 } })
    await vi.waitFor(() => {
      expect(root.querySelector<HTMLElement>(`#${CLAIM_ID}`)?.textContent).toContain(
        '移动端 30 单',
      )
    })
  })

  it('没有节点 ⇒ 一句提示、不注入标记、不发业务请求', async () => {
    mockedFetchMe.mockResolvedValue(me([]))
    await mountPage()
    expect(LAYUI.messages).toEqual([NO_ACCESS_MESSAGE])
    expect(root.querySelector(PANEL_SELECTOR)).toBeNull()
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('请求失败 ⇒ 错误文案上屏（不把空数据当 0）', async () => {
    mockedRequest.mockRejectedValueOnce(new Error('后端炸了'))
    await mountPage()
    expect(root.querySelector<HTMLElement>(`#${ERROR_ID}`)?.textContent).toBe('后端炸了')
  })
})
