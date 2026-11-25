// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【自动发消息到微信群设置】页（2026-10-17 实装）的判据。
 *
 * 关键点（都是**能失败**的判据）：
 * - 两种渠道（企微 / 自定义）都在弹窗里可选，说明文字跟着渠道变；
 * - `发交易平台时` 这一档**必须**在页面上说破「今天不会发」（交易平台 M6 未做）；
 * - 表单校验：空 URL / 非 http(s) / 一个游戏都没勾 / 没选时机，四条都拦住；
 * - 表格里 Webhook **只显示 host**（URL 里带凭据，不能整串上屏）；
 * - 挂载后拉的列表走 `/api/business/order-push-settings/`，开关走 PATCH、测试走 `/test/`。
 */

const API = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  test: vi.fn(),
}))

const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (obj: unknown) => unknown>()
  const formHandlers = new Map<string, (obj: unknown) => unknown>()
  const opens: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  let opened: { host: HTMLElement; opts: Record<string, unknown> } | null = null
  return {
    msg,
    tableHandlers,
    formHandlers,
    opens,
    reloads,
    form: {
      render: vi.fn(),
      on: vi.fn((event: string, cb: (obj: unknown) => unknown) => {
        formHandlers.set(event, cb)
      }),
    },
    table: {
      render: vi.fn(),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
      }),
      on: vi.fn(),
    },
    layer: {
      open: vi.fn((opts: Record<string, unknown>) => {
        opens.push(opts)
        const host = document.createElement('div')
        host.className = 'layui-layer'
        const content = document.createElement('div')
        content.className = 'layui-layer-content'
        content.innerHTML = String(opts.content ?? '')
        host.appendChild(content)
        document.body.appendChild(host)
        opened = { host, opts }
        return opens.length
      }),
      close: vi.fn(() => {
        if (!opened) return
        ;(opened.opts.end as (() => void) | undefined)?.()
        opened.host.remove()
        opened = null
      }),
      msg,
      confirm: vi.fn((_text: string, _opts: unknown, yes: (i: number) => void) => yes(1)),
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: LAYUI.form,
    table: LAYUI.table,
    layer: LAYUI.layer,
  })),
  initIslandElements: vi.fn(),
}))
vi.mock('@/api/config', () => ({ fetchAllGames: vi.fn() }))
vi.mock('@/api/orderPush', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/orderPush')>()),
  listOrderPushSettings: API.list,
  createOrderPushSetting: API.create,
  updateOrderPushSetting: API.update,
  deleteOrderPushSetting: API.remove,
  testOrderPushSetting: API.test,
}))

import { fetchAllGames } from '@/api/config'
import { timingHint } from '@/api/orderPush'
import page, {
  CHANNEL_LABELS,
  dialogHtml,
  gamesLabel,
  parseGameIds,
  validatePushForm,
  webhookHost,
} from '../system-orderpushgroupmsgset'

const ROWS = [
  {
    id: 1,
    name: '大厅提醒',
    channel: 'wecom' as const,
    webhook_url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET',
    games: [1, 2],
    timing: 10 as const,
    msg_type: 'markdown' as const,
    call_all: false,
    remark: '秒回',
    is_active: true,
    created_at: '',
    updated_at: '',
  },
]

const GAMES = [
  { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
  { id: 2, name: '鸣潮', sort_order: 0, is_active: true, created_at: '' },
]

beforeEach(() => {
  LAYUI.msg.mockReset()
  LAYUI.opens.length = 0
  LAYUI.reloads.length = 0
  LAYUI.tableHandlers.clear()
  document.body.innerHTML = ''
  API.list.mockReset()
  API.update.mockReset()
  API.test.mockReset()
  API.list.mockResolvedValue({ items: ROWS, total: 1, page: 1, page_size: 200 })
  API.update.mockResolvedValue(ROWS[0])
  ;(fetchAllGames as ReturnType<typeof vi.fn>).mockResolvedValue(GAMES)
})

describe('纯函数', () => {
  it('Webhook 上屏只显示 host（key 是凭据，不整串显示）', () => {
    expect(webhookHost('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET')).toBe(
      'qyapi.weixin.qq.com/…',
    )
    expect(webhookHost('not-a-url')).toBe('—')
  })

  it('限定游戏按全量游戏顺序拼名字，没有命中就显示 —', () => {
    expect(gamesLabel([2, 1], GAMES)).toBe('原神、鸣潮')
    expect(gamesLabel([], GAMES)).toBe('—')
    expect(gamesLabel([99], GAMES)).toBe('—')
  })

  it('校验四条：URL 空 / 非 http(s) / 没勾游戏 / 没选时机', () => {
    expect(validatePushForm({ webhookUrl: '', games: [1], timing: 10 })).toBe(
      'Webhook 地址不能为空',
    )
    expect(validatePushForm({ webhookUrl: 'ftp://x', games: [1], timing: 10 })).toBe(
      'Webhook 地址必须是 http:// 或 https:// 开头的完整地址',
    )
    expect(
      validatePushForm({ webhookUrl: 'https://a.example/hook', games: [], timing: 10 }),
    ).toBe('至少要选择一个游戏')
    expect(
      validatePushForm({ webhookUrl: 'https://a.example/hook', games: [1], timing: null }),
    ).toBe('请选择发送时机')
    expect(
      validatePushForm({ webhookUrl: 'https://a.example/hook', games: [1], timing: 30 }),
    ).toBeNull()
  })

  it('parseGameIds 丢掉非数字 / 0 / 负数', () => {
    expect(parseGameIds(['1', '2', 'x', '0', '-3'])).toEqual([1, 2])
  })

  it('「发交易平台时」这一档必须说破今天不会发', () => {
    expect(timingHint(20)).toContain('交易平台还没对接')
    expect(timingHint(20)).toContain('暂时不会发送')
    for (const t of [0, 10, 30] as const) {
      expect(timingHint(t)).not.toContain('暂时不会发送')
    }
  })

  it('弹窗：两档渠道都在，规则名称 / Webhook / 游戏 / 时机 / @全员 / 消息类型 / 备注齐全', () => {
    const html = dialogHtml(ROWS[0]!, GAMES)
    expect(html).toContain('value="wecom"')
    expect(html).toContain('value="custom"')
    expect(html).toContain(CHANNEL_LABELS.wecom)
    expect(html).toContain(CHANNEL_LABELS.custom)
    for (const name of ['name', 'webhook_url', 'game', 'timing', 'call_all', 'msg_type', 'remark']) {
      expect(html).toContain(`name="${name}"`)
    }
    // 编辑回填：游戏 1/2 都勾上、时机 10、备注「秒回」
    expect(html).toContain('value="1" checked')
    expect(html).toContain('value="2" checked')
    expect(html).toContain('value="10" title="只发内部抢单大厅时"')
    expect(html).toContain('value="秒回"')
  })
})

describe('挂载', () => {
  it('装载即拉规则列表，表格数据、规则名、渠道、游戏名都在', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })
    expect(API.list).toHaveBeenCalledWith({ page: 1, page_size: 200 })
    const reload = LAYUI.reloads.find((r) => r.id === 'tableList')
    expect(reload?.data).toHaveLength(1)
    expect(root.querySelector('#pushTotal')!.textContent).toBe('共 1 条')
    unmount()
  })

  it('开关走 PATCH；测试按钮走 `/test/`（不是又一次 PATCH）', async () => {
    API.test.mockResolvedValue({ ok: true })
    const root = document.createElement('div')
    document.body.appendChild(root)
    const unmount = await page.mount(root, { onNavigate: () => undefined })

    const switchHandler = LAYUI.formHandlers.get('switch(pushEnabled)')
    expect(switchHandler).toBeDefined()
    switchHandler!({ elem: { value: '1', checked: false } })
    await Promise.resolve()
    expect(API.update).toHaveBeenCalledWith(1, { is_active: false })

    // 行内「测试」（表格由 layui 渲染；替身不画行 ⇒ 自己塞一颗同样 data-act 的按钮进 root，
    // 判据是**同一个点击委托**，不是 layui 的 DOM 长相）
    const testBtn = document.createElement('a')
    testBtn.dataset.act = 'test'
    testBtn.dataset.id = '1'
    root.appendChild(testBtn)
    testBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await Promise.resolve()
    expect(API.test).toHaveBeenCalledWith(1)
    unmount()
  })
})
