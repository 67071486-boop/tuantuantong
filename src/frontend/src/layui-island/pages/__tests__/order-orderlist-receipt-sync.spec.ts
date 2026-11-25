// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  defaultReceiptSyncRange,
  injectReceiptSyncButton,
  RECEIPT_SYNC_ACCEPTED_TEXT,
  RECEIPT_SYNC_BUTTON_HTML,
  RECEIPT_SYNC_DIALOG_TITLE,
  RECEIPT_SYNC_EVENT,
  RECEIPT_SYNC_LABEL,
  RECEIPT_SYNC_MAX_DAYS,
  receiptSyncRangeError,
  setOrderListPermissions,
} from '../order-orderlist'
import { request } from '@/api/http'
import { BizError } from '@/api/errors'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-orderlist.html`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

const URL = '/api/business/orders/xgj-receipt-sync/'

/**
 * 订单管理页「同步收款」（2026-10-26；计划 `docs/plans/2026-10-26-receipt-sync-button.md`）。
 *
 * 覆盖计划 §6 里属于前端的四条：
 * - ⑩ 按钮**按节点渲染**（`order.change_payment_status` fail-closed，「不注入」那半）；
 * - ⑥「范围 > 1 个月」的**前端同一判据**（纯函数）；
 * - 「点击后立即返回不阻塞」的**客户端形状**（弹层提交只发一发、成功只提示一句）；
 * - 默认范围 = 最近 7 天（含今天）。
 *
 * ⚠️ 传输层替身：只替 `request()`，`api/orders.ts::startXgjReceiptSync` **保持真身**
 * ⇒ 「打的是哪个 URL / 载荷是什么」断言在真出口上（E-0009 的同族纪律）。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const layers = new Map<number, { node: HTMLElement | null; end?: () => void }>()
  let nextIndex = 1
  const openImpl = (opts: unknown): number => {
    const cfg = (opts ?? {}) as {
      content?: unknown
      end?: () => void
      success?: () => void
    }
    const index = nextIndex++
    const layer = document.createElement('div')
    layer.className = 'layui-layer'
    const content = document.createElement('div')
    content.className = 'layui-layer-content'
    layer.appendChild(content)
    document.body.appendChild(layer)
    // 本页「同步收款」弹层改用**字符串 content**（真 layer 会自己建 DOM）；
    // 其余弹层仍可能传节点 / jQuery 包装 ⇒ 三种形状都接住。
    const contentOf = (value: unknown): HTMLElement | null => {
      if (value instanceof HTMLElement) return value
      const wrapped = (value as { node?: unknown } | null)?.node
      return wrapped instanceof HTMLElement ? wrapped : null
    }
    let node = contentOf(cfg.content)
    if (node === null && typeof cfg.content === 'string') {
      content.innerHTML = cfg.content
      node = content.firstElementChild instanceof HTMLElement ? content.firstElementChild : null
    }
    if (node) content.appendChild(node)
    layers.set(index, { node, end: cfg.end })
    // ⚠️ 真 layer 在内容进 DOM 之后才调 `success` ⇒ 本页的「同步收款」弹层靠它绑事件。
    cfg.success?.()
    return index
  }
  // 用 `vi.fn` 包一层：既要真行为（搬节点），又要能断言 / 清空调用记录。
  const open = vi.fn(openImpl)
  return {
    msg,
    open,
    //: layui 的 jQuery 包装（本页三个弹层都用它包节点）。只做「可识别包装」，不模拟 jQuery。
    table: {
      render: vi.fn(),
      on: vi.fn(),
      reload: vi.fn(),
      checkStatus: vi.fn((): { data: unknown[] } => ({ data: [] })),
      getOptions: vi.fn((): { page: { limit: number } } => ({ page: { limit: 20 } })),
    },
    layer: {
      open,
      msg,
      close: vi.fn((index: number) => {
        const rec = layers.get(index)
        if (rec === undefined) return
        layers.delete(index)
        rec.node?.closest('.layui-layer')?.remove()
        rec.end?.()
      }),
      load: vi.fn(() => 9002),
      prompt: vi.fn(),
      tips: vi.fn(),
      confirm: vi.fn(),
      alert: vi.fn(),
    },
    form: { render: vi.fn(), on: vi.fn(), val: vi.fn(() => ({})) },
    laydate: { render: vi.fn() },
    //: 真 layui 的 jQuery 包装（`layer.open({type:1})` 会用它包节点，见弹层那条断言）。
    //: 测试替身「原样返回」即可：判据只钉「源码有没有调它」，⛔ 不钉返回形状。
    //: 真 layui 的 jQuery 包装：`layer.open({type:1})` 拿到的是**包装对象**、不是裸节点
    //: （它会调 `content.parents(...)`）。替身用 `{ node }` 模拟这个形状。
    $: vi.fn((el: Element) => ({ node: el })),
    element: { init: vi.fn() },
    __resetLayers: (): void => {
      layers.clear()
      nextIndex = 1
    },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return { ...actual, bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland }
})

vi.mock('@/layui/loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/loader')>()
  return { ...actual, loadLayui: async () => LAYUI }
})

vi.mock('@/api/http', () => ({ request: vi.fn(), http: { get: vi.fn() } }))

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return {
    ...actual,
    fetchFilterOptions: vi.fn(async () => ({
      takers: [],
      publishers: [],
      takers_truncated: false,
      publishers_truncated: false,
    })),
    fetchAssigneeOptions: vi.fn(),
  }
})

vi.mock('@/api/orderSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orderSources')>()
  return {
    ...actual,
    fetchOrderSources: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
  }
})

/** `/api/auth/me` 的载荷：带**收款节点**（按钮该出现的那一档）。 */
const ME_WITH_NODE = {
  is_tenant_admin: false,
  is_account_admin: false,
  menu_permissions: ['order.list', 'order.change_payment_status'],
  roles: [],
}
/** 同一形状但不带收款节点（按钮**不该**出现）。 */
const ME_WITHOUT_NODE = {
  is_tenant_admin: false,
  is_account_admin: false,
  menu_permissions: ['order.list'],
  roles: [],
}

function mockRequests(me: unknown): void {
  vi.mocked(request).mockImplementation((async (config: { url?: string }) => {
    if (config?.url === '/api/auth/me') return { user: me }
    if (config?.url === URL) return { accepted: true }
    return {}
  }) as never)
}

/** 等一拍微任务（弹层提交后的 `.then` 链）。 */
const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

describe('「同步收款」的范围判据（纯函数，前后端同宽）', () => {
  it('默认范围 = 最近 7 天（含今天）', () => {
    // ⚠️ 用**构造出来的本地日**，⛔ 不依赖跑测试那天（跨午夜跑也不飘）。
    expect(defaultReceiptSyncRange(new Date(2026, 9, 26))).toEqual({
      from: '2026-10-20',
      to: '2026-10-26',
    })
  })

  it('含首尾恰好 31 天放行、32 天拒绝（拒绝话里点名 31）', () => {
    expect(receiptSyncRangeError('2026-10-01', '2026-10-31')).toBeNull()
    const error = receiptSyncRangeError('2026-10-01', '2026-11-01')
    expect(error).not.toBeNull()
    expect(error).toContain(String(RECEIPT_SYNC_MAX_DAYS))
  })

  it('开始晚于结束 ⇒ 拒绝；空 / 坏格式 ⇒ 拒绝（⛔ 不发请求）', () => {
    expect(receiptSyncRangeError('2026-10-27', '2026-10-26')).toBe('开始日期不能晚于结束日期')
    expect(receiptSyncRangeError('', '2026-10-26')).not.toBeNull()
    expect(receiptSyncRangeError('2026-10-26', '2026-13-01')).not.toBeNull()
    expect(receiptSyncRangeError('2026-10-26', '20261026')).not.toBeNull()
  })
})

describe('按钮注入：按节点 fail-closed（D-0154）', () => {
  /** 一个最小的工具栏容器（证据件里那颗「刷新」的反查锚点）。 */
  function toolbarRoot(): HTMLElement {
    const root = document.createElement('div')
    root.innerHTML =
      '<div class="layui-btn-group"><button data-tb="refresh" type="button">刷新</button></div>'
    return root
  }

  it('有 order.change_payment_status ⇒ 注入一颗与旁边同款式的按钮', () => {
    setOrderListPermissions({ menu_permissions: ['order.change_payment_status'] })
    const root = toolbarRoot()
    expect(injectReceiptSyncButton(root)).toBe(true)
    const button = root.querySelector<HTMLElement>(`[data-tb="${RECEIPT_SYNC_EVENT}"]`)
    expect(button).not.toBeNull()
    expect(button!.className).toBe('layui-btn layui-btn-sm')
    expect(button!.textContent).toBe(RECEIPT_SYNC_LABEL)
    expect(RECEIPT_SYNC_BUTTON_HTML).toContain(`data-tb="${RECEIPT_SYNC_EVENT}"`)
  })

  it('没有该节点 ⇒ 不注入（点了必 403 的入口就不该出现）', () => {
    setOrderListPermissions({ menu_permissions: ['order.list'] })
    const root = toolbarRoot()
    expect(injectReceiptSyncButton(root)).toBe(false)
    expect(root.querySelector(`[data-tb="${RECEIPT_SYNC_EVENT}"]`)).toBeNull()
  })

  it('身份取不到（fail-closed）⇒ 不注入', () => {
    setOrderListPermissions(undefined)
    const root = toolbarRoot()
    expect(injectReceiptSyncButton(root)).toBe(false)
  })
})

describe('挂载级：注入 → 弹层 → 点「开始同步」', () => {
  beforeEach(() => {
    LAYUI.msg.mockClear()
    LAYUI.layer.open.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.__resetLayers()
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE }
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.mocked(request).mockReset()
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    return { root, cleanup }
  }

  function lastRequestConfig(): { url?: string; method?: string; data?: unknown } {
    const calls = vi.mocked(request).mock.calls
    const hit = [...calls].reverse().find((call) => {
      const config = call[0] as { url?: string } | undefined
      return config?.url === URL
    })
    return (hit?.[0] ?? {}) as { url?: string; method?: string; data?: unknown }
  }

  it('有权限：挂载后按钮在工具栏里；点开弹层默认两个日期，提交只发一发、立刻提示', async () => {
    mockRequests(ME_WITH_NODE)
    const { root, cleanup } = await mountPage()

    const button = root.querySelector<HTMLElement>(`[data-tb="${RECEIPT_SYNC_EVENT}"]`)
    expect(button, '有权限却没注入「同步收款」').not.toBeNull()
    button!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const first = LAYUI.layer.open.mock.calls[0]?.[0] as { title: string; content?: unknown }
    expect(first.title).toBe(RECEIPT_SYNC_DIALOG_TITLE)
    // ⭐ 线上 bug（`Uncaught TypeError: r.parents is not a function`）的钉子：
    //    真 layui 的 `layer.open({type:1})` 会拿 `content` 调 `content.parents(...)`
    //    ⇒ **必须**先用 `layui.$(node)` 包一层 jQuery（本页另两个弹层同此写法）；
    //    直接传原生 `HTMLElement` 时弹层**根本不出现**（点了像没反应）。
    //    ⚠️ 测试替身里 `layui.$` 是「原样返回」，所以这里钉的是 **「确实调用过 $ 包装」**
    //    （⛔ 不钉返回形状：那会把替身与真 jQuery 的差异写进判据）。
    //    ⛔ 把源码改回 `content: node` ⇒ 本行必红。
    // ⭐ 线上实测钉子：本弹层**必须给字符串 content**。
    //    传原生节点 ⇒ layer 抛 `r.parents is not a function`（弹层不出现）；
    //    传 `layui.$(node)` ⇒ 这页只出阴影、内容不渲染。改成字符串后两者都不会发生。
    //    ⛔ 把源码改回节点/包装 ⇒ 本行必红。
    expect(typeof first.content, '弹层 content 必须是 HTML 字符串（节点/包装都会让 layer 不显示内容）').toBe(
      'string',
    )
    expect(String(first.content)).toContain('data-receipt-sync')

    const dialog = document.querySelector<HTMLElement>('[data-receipt-sync]')
    expect(dialog).not.toBeNull()
    const fromInput = dialog!.querySelector<HTMLInputElement>('[data-field="date-from"]')!
    const toInput = dialog!.querySelector<HTMLInputElement>('[data-field="date-to"]')!
    expect(fromInput.value).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(toInput.value).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(receiptSyncRangeError(fromInput.value, toInput.value)).toBeNull()

    // 改成 32 天：前端先拦，一发请求都不发。
    fromInput.value = '2026-10-01'
    toInput.value = '2026-11-01'
    dialog!
      .querySelector<HTMLElement>('[data-receipt-action="start"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(lastRequestConfig().url).toBeUndefined()
    expect(LAYUI.msg.mock.calls.some((call) => String(call[0]).includes('31'))).toBe(true)

    // 合法范围：发一发、载荷是两个日期，成功后关层 + 那句「已开始同步」。
    fromInput.value = '2026-10-01'
    toInput.value = '2026-10-31'
    LAYUI.msg.mockClear()
    dialog!
      .querySelector<HTMLElement>('[data-receipt-action="start"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()

    const config = lastRequestConfig()
    expect(config.method).toBe('post')
    expect(config.data).toEqual({ date_from: '2026-10-01', date_to: '2026-10-31' })
    expect(LAYUI.msg).toHaveBeenCalledWith(RECEIPT_SYNC_ACCEPTED_TEXT, expect.anything())
    expect(LAYUI.layer.close).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('后端回 1037 ⇒ 页面原样显示后端那句人话（⛔ 不自己拼）', async () => {
    mockRequests(ME_WITH_NODE)
    vi.mocked(request).mockImplementation((async (config: { url?: string }) => {
      if (config?.url === '/api/auth/me') return { user: ME_WITH_NODE }
      if (config?.url === URL) throw new BizError(1037, '同步收款正在运行，请稍后再试')
      return {}
    }) as never)
    const { root, cleanup } = await mountPage()

    root
      .querySelector<HTMLElement>(`[data-tb="${RECEIPT_SYNC_EVENT}"]`)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const dialog = document.querySelector<HTMLElement>('[data-receipt-sync]')!
    dialog
      .querySelector<HTMLElement>('[data-receipt-action="start"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()

    expect(LAYUI.msg).toHaveBeenCalledWith(
      '同步收款正在运行，请稍后再试',
      expect.anything(),
    )
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    cleanup()
  })

  it('没有收款节点：整页挂载后**没有**这颗按钮', async () => {
    mockRequests(ME_WITHOUT_NODE)
    const { root, cleanup } = await mountPage()
    expect(root.querySelector(`[data-tb="${RECEIPT_SYNC_EVENT}"]`)).toBeNull()
    cleanup()
  })
})
