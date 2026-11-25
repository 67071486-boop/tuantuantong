// @vitest-environment jsdom
/**
 * 订单详情页 **T4「左下三颗按钮」** 的挂载层接线测试（layui 换可控替身，手法同
 * `order-orderview-logs.spec.ts`）。
 *
 * 为什么单开一个文件：`order-orderview.spec.ts` 里有 4 条**页内 `<style>` 的逐字钉子**
 * （评审 I-1），本轮的用例与它们无关；混在一个文件里容易在改夹具时碰坏那几段字面量。
 *
 * 断言都打在**真实出口**上（AGENTS.md §5 / E-0009）：
 *  - 「复制订单内容」→ **`layui.lay.clipboard.writeText`** 的**入参**（文本 + `done` / `error` 回调），
 *    以及回调触发后 `layer.msg` 的**字面文案**（⚠️ **不是** `layui.util.clipboard`——实测 `util`
 *    没有 `clipboard` 键，见 `order-orderview.ts` 文件头与 T4 执行记录）；
 *  - 「继续拆分订单」→ 打开拆单弹窗、填写来源内容/子单接单价，再核对 `split-peer` 的真实请求；
 *  - 「刷新本界面」→ 证据件里那颗 `<a>` 的**字面 `href`**（本轮有意**不接线**，见下）。
 *
 * ⚠️ 复制文本的期望值是**整段字面量、含行序与 `\n`**（不引用生产常量，E-0024）——
 * 少一行 / 换行符改成别的都会红。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const EVIDENCE = readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/order-orderview.html',
  ),
  'utf8',
)

/** layui 的可控替身（`vi.hoisted`：`vi.mock` 的工厂会被提升到 import 之前）。 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const writeText = vi.fn()
  const prompt = vi.fn()
  const open = vi.fn((_options: unknown) => 9)
  return {
    msg,
    writeText,
    prompt,
    form: { render: vi.fn(), on: vi.fn() },
    layer: { msg, prompt, open, close: vi.fn() },
    table: { render: vi.fn(), reloadData: vi.fn(), on: vi.fn() },
    // 「复制订单内容」的出口就是它——⚠️ **`layui.lay`，不是 `layui.util`**（vendored 2.13.9 实测：
    // `layui.lay.clipboard.writeText` 存在、`layui.util.clipboard` 是 `undefined`）。
    lay: { clipboard: { writeText } },
    // 真 layui 里 `util` 存在但**没有** `clipboard`；替身保留一个空对象，钉住「不会去 util 上找」。
    util: {},
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import type { Order } from '@/api/orders'
import { mountOrderOrderViewPage } from '../order-orderview'

const mockedRequest = vi.mocked(request)

/** 20 位系统单号（**合成值**）。 */
const ORDER_NO = '20260922120000000001'

/** 一单真数据（形状同后端 `OrderSerializer`，值与 `order-orderview.spec.ts` 的夹具同形）。 */
function makeOrder(): Order {
  return {
    order_no: ORDER_NO,
    status: 'COMPLETED',
    can_continue_split: true,
    order_type: 'BOOST',
    game: 11,
    server: 22,
    category: 33,
    project: 44,
    project_text: '三角洲行动 排位上分',
    // ⭐ `Order` 有这一列（`OrderSerializer` 是 `exclude = []`）。
    split_id: '',
    content: '',
    demand: '',
    time_requirement: '',
    game_account: 'acc_demo_01',
    game_sub_account: 'sub_demo_02',
    game_role_name: '角色甲',
    current_info_1: '',
    current_info_2: '',
    customer_phone: '',
    customer_qq: '',
    customer_ww: '',
    total_price: '199.50',
    received_price: null,
    platform_price: null,
    hall_price: null,
    refund_fee: '0.00',
    fee: null,
    payment_method: '',
    // T8 起 `Order.payment_status` 是必填键（后端三档码，default `UNSETTLED`）⇒ 夹具补齐。
    payment_status: 'UNSETTLED',
    source: '',
    order_source: null,
    source_order_no: '',
    sales_agent: '',
    is_urgent: false,
    remark: '',
    tag: '',
    created_by: '',
    updated_by: '',
    created_at: '2026-09-20T21:32:15+08:00',
    updated_at: '2026-09-20T21:32:15+08:00',
    allowed_transitions: [],
    // 批 F / F3 起详情响应多一个只读布尔键（后端 `OrderSerializer` 恒回它）。
    completed_requires_review: false,
  }
}

type ReqConfig = { method?: string; url?: string; data?: Record<string, unknown>; params?: Record<string, unknown> }
let orderDetailOverride: Order | null = null
let groupMembersOverride: Record<string, unknown>[] | null = null

function installRequest(): void {
  mockedRequest.mockImplementation((async (config: ReqConfig) => {
    const url = config.url ?? ''
    if (url.endsWith('/audit/')) return []
    // 拆单金额接口回包（`{split_id, split_kind, source_total_price, created}`）。
    if (url.endsWith('/split-peer/')) {
      return { split_id: '90000000000000000001', split_kind: 'PEER', source_total_price: '179.50', created: [{ order_no: '20260922120000000002' }] }
    }
    if (url === '/api/business/orders/' && config.params?.split_id !== undefined) {
      return { items: groupMembersOverride ?? [], total: groupMembersOverride?.length ?? 0, page: 1, page_size: 200 }
    }
    if (url.startsWith('/api/business/orders/')) return orderDetailOverride ?? makeOrder()
    // Task 5：`fetchGames()` 先读 `/api/auth/me` 的 `user.game_ids`；本夹具的字典只有 id=11
    // （下面那行 games）⇒ 权限钉字面 [11]，否则游戏名字典为空。
    // ⭐ 平台写按钮的节点门（降级档按 `order.list`，见 perms.ts）。
    if (url === '/api/auth/me') return { user: { game_ids: [11], menu_permissions: ['order.list'] } }
    // 4 个 FK 字典接口：回那几行，让「对标行」显示名字（文本断言要用到）。
    if (url === '/api/business/games/') return { items: [{ id: 11, name: '三角洲行动' }] }
    if (url === '/api/business/servers/') return { items: [{ id: 22, name: '安卓微信区' }] }
    if (url === '/api/business/categories/') return { items: [{ id: 33, name: '排位上分' }] }
    if (url === '/api/business/projects/') return { items: [{ id: 44, name: '钻石到星耀' }] }
    throw new Error(`未预期的 request：${url}`)
  }) as never)
}

async function mountPage(): Promise<HTMLElement> {
  window.history.replaceState({}, '', `/layui-island/page.html?order_no=${ORDER_NO}`)
  const root = document.createElement('div')
  document.body.appendChild(root)
  await mountOrderOrderViewPage(root, { onNavigate: vi.fn() })
  return root
}

beforeEach(() => {
  vi.clearAllMocks()
  orderDetailOverride = null
  groupMembersOverride = null
  installRequest()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-orderview.html')) {
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }
      if (String(url).includes('/api/auth/me')) {
        return { ok: true, json: async () => ({ code: 0, data: { user: { menu_permissions: ['order.view_detail', 'order.add_edit', 'order.edit_payout_price'] } } }) }
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('刷新本界面：**不接线**，证据件原文一字未改（「已知会不同」第 32 条）', () => {
  it('那颗 `<a>` 仍是 `href="javascript:location.reload(true);"`（在 island iframe 里 = 重载本页 ⇒ 重新取数）', async () => {
    const root = await mountPage()
    const refresh = Array.from(root.querySelectorAll<HTMLAnchorElement>('a')).find(
      (a) => (a.textContent ?? '').trim() === '刷新本界面',
    )
    expect(refresh, '证据件里没有「刷新本界面」这颗按钮').not.toBeUndefined()
    // ⛔ 字面 `href`：改成 `javascript:void(0)` + 挂 JS 就等于改掉了对标的标记（第 32 条）。
    expect(refresh!.getAttribute('href')).toBe('javascript:location.reload(true);')
  })
})

describe('复制订单内容：`layui.lay.clipboard.writeText`（自拼的整单文本，第 33 条）', () => {
  /**
   * 页面渲染完之后，`buildCopyText` 应当拼出的整段文本——**字面量**：14 行、行序照
   * `STANDARD_ROW_LABELS`、`\n` 连接。值取夹具 + 4 个字典（见 `installRequest`）。
   */
  const EXPECTED_TEXT = [
    '订单号：20260922120000000001',
    '游戏区服：三角洲行动/安卓微信区',
    '角色信息：角色甲',
    '游戏账号：acc_demo_01',
    '子账号：sub_demo_02',
    '登录密码：',
    '暗号/口令：',
    '项目分类：排位上分',
    '代练项目：三角洲行动 排位上分',
    '订单状态：已完成',
    // 第 2 格里的「还剩」是对标原文的字面量（第 6 条登记）——它会出现在复制的文本里。
    '订单计时：还剩',
    '当前接手：',
    '联系电话：',
    '指定电话：',
  ].join('\n')

  it('点 `#copybtn` ⇒ `writeText` 收到**由页面渲染出的行拼成的字面文本**（含行序与 `\\n`）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>('#copybtn')!.click()

    expect(LAYUI.writeText).toHaveBeenCalledTimes(1)
    const options = LAYUI.writeText.mock.calls[0]![0] as {
      text: string
      done: unknown
      error: unknown
    }
    expect(options.text).toBe(EXPECTED_TEXT)
    // 逐段再核一次「换行符就是 `\n`」（把分隔符改成 `|` / `、` 这类都过不了上面那一条，
    // 这里额外把行数也钉住，报错信息更直白）。
    expect(options.text.split('\n')).toHaveLength(14)
    expect(options.done).toBeTypeOf('function')
    expect(options.error).toBeTypeOf('function')
    // `data-clipboard-text` **保持证据件的空串**（不改标记；文本由运行期自拼）。
    expect(root.querySelector<HTMLElement>('#copybtn')!.getAttribute('data-clipboard-text')).toBe(
      '',
    )
  })

  it('`done` 回调弹对标那句成功提示（逐字）；`error` 回调弹失败提示 + 第二参 `\'error\'`', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>('#copybtn')!.click()
    const options = LAYUI.writeText.mock.calls[0]![0] as {
      done: () => void
      error: () => void
    }
    options.done()
    expect(LAYUI.msg).toHaveBeenCalledWith('已成功复制订单内容到剪贴板')
    options.error()
    expect(LAYUI.msg).toHaveBeenCalledWith('复制订单内容失败了，请手动复制', 'error')
  })

  it('复制**零请求**：不重取订单 / 日志，也不上报复制留痕（对标还有 `POST CopyOrderLog`，我们没有那条端点）', async () => {
    const root = await mountPage()
    const before = mockedRequest.mock.calls.length
    root.querySelector<HTMLElement>('#copybtn')!.click()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })
})

describe('继续拆分订单：真实弹窗提交金额拆分请求', () => {
  it('点 `.spliteorder` ⇒ 编辑来源和新增子单 ⇒ 校验真实请求载荷', async () => {
    const root = await mountPage()
    type Sent = { method?: string; url?: string; data?: Record<string, unknown> }
    const splitConfigs = (): Sent[] =>
      mockedRequest.mock.calls
        .map((call) => call[0] as Sent)
        .filter((config) => String(config.url ?? '').endsWith('/split-peer/'))
    const split = root.querySelector<HTMLElement>('.spliteorder')
    expect(split, '证据件里没有 .spliteorder').not.toBeNull()
    split!.click()

    await vi.waitFor(() => expect(LAYUI.layer.open).toHaveBeenCalledTimes(1))
    const layerOptions = LAYUI.layer.open.mock.calls[0]![0] as { content: string; success?: () => void }
    document.body.insertAdjacentHTML('beforeend', `<div class="layui-layer-content">${layerOptions.content}</div>`)
    layerOptions.success?.()
    const panel = document.querySelector<HTMLElement>('[data-peer-split]')!
    panel.querySelector<HTMLTextAreaElement>('[data-remaining-project]')!.value = '来源剩余项目'
    panel.querySelector<HTMLElement>('[data-add-new]')!.click()
    panel.querySelector<HTMLTextAreaElement>('[data-new-project]')!.value = '新增项目'
    panel.querySelector<HTMLInputElement>('[data-new-price]')!.value = '20.00'
    panel.querySelector<HTMLElement>('[data-save-split]')!.click()
    // ⚠️ 判据打在**真实出口**上：`splitPeerOrder()` → `request()` 的 URL / 方法 / 载荷。
    await vi.waitFor(() => {
      expect(splitConfigs()).toHaveLength(1)
    })
    const call = splitConfigs()[0]!
    expect(call.method).toBe('post')
    expect(call.url).toBe(`/api/business/orders/${ORDER_NO}/split-peer/`)
    expect(call.data).toEqual({
      remaining_project_text: '来源剩余项目',
      new_orders: [{ project_text: '新增项目', total_price: '20.00' }],
    })
    // 成功文案（后端成功信封的 `msg` 是空串 ⇒ 这句是页内自撰）
    await vi.waitFor(() => {
      const texts = LAYUI.msg.mock.calls.map((c) => String(c[0]))
      expect(texts.some((t) => t.startsWith('拆分成功'))).toBe(true)
    })
  })

  it('不新增也不改内容 ⇒ 提示并且零拆分请求', async () => {
    const root = await mountPage()
    const splitCalls = (): ReqConfig[] => mockedRequest.mock.calls
      .map((call) => call[0] as ReqConfig)
      .filter((config) => String(config.url ?? '').endsWith('/split-peer/'))
    root.querySelector<HTMLElement>('.spliteorder')!.click()
    await vi.waitFor(() => expect(LAYUI.layer.open).toHaveBeenCalledTimes(1))
    const layerOptions = LAYUI.layer.open.mock.calls[0]![0] as { content: string; success?: () => void }
    document.body.insertAdjacentHTML('beforeend', `<div class="layui-layer-content">${layerOptions.content}</div>`)
    layerOptions.success?.()
    const beforeSave = mockedRequest.mock.calls.length
    document.querySelector<HTMLElement>('[data-peer-split] [data-save-split]')!.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(LAYUI.msg).toHaveBeenCalledWith('请修改发起订单内容、已有子单内容或新增拆出订单', { time: 6000 })
    expect(splitCalls()).toHaveLength(0)
    expect(mockedRequest.mock.calls.length).toBe(beforeSave)
  })

  it('从子单打开时锚点置顶，新增金额从发起子单扣并守恒', async () => {
    const anchorNo = '20260922120000000002'
    const groupId = 'peer-v1-child-source'
    const child = { ...makeOrder(), status: 'IN_PROGRESS', split_id: groupId, split_kind: 'PEER' as const, project_text: '发起子单项目', total_price: '10.00' }
    orderDetailOverride = child
    groupMembersOverride = [
      { ...child, order_no: ORDER_NO, split_is_balance_anchor: false },
      { ...child, order_no: anchorNo, project_text: '原单项目', total_price: '35.00', split_is_balance_anchor: true },
    ]
    const root = await mountPage()
    root.querySelector<HTMLElement>('.spliteorder')!.click()
    await vi.waitFor(() => expect(LAYUI.layer.open).toHaveBeenCalledTimes(1))
    const options = LAYUI.layer.open.mock.calls[0]![0] as { content: string; success?: () => void }
    document.body.insertAdjacentHTML('beforeend', `<div class="layui-layer-content">${options.content}</div>`)
    options.success?.()
    const panel = document.querySelector<HTMLElement>('[data-peer-split]')!
    expect(panel.querySelectorAll('.tt-split-member')[0]!.textContent).toContain(anchorNo)
    const mainProject = panel.querySelector<HTMLTextAreaElement>(`[data-order="${anchorNo}"] [data-existing-project]`)!
    expect(mainProject.value).toBe('原单项目')
    mainProject.value = '原单项目已改'
    const sourcePrice = panel.querySelector<HTMLInputElement>('[data-source-price]')!
    sourcePrice.value = '12.00'
    sourcePrice.dispatchEvent(new Event('input', { bubbles: true }))
    panel.querySelector<HTMLElement>('[data-add-new]')!.click()
    panel.querySelector<HTMLTextAreaElement>('[data-new-project]')!.value = '新增项目'
    const newPrice = panel.querySelector<HTMLInputElement>('[data-new-price]')!
    newPrice.value = '5.00'
    newPrice.dispatchEvent(new Event('input', { bubbles: true }))
    expect(panel.querySelector<HTMLElement>('[data-total-summary]')!.textContent).toContain('原单预计 33.00')
    expect(panel.querySelector<HTMLElement>('[data-total-summary]')!.textContent).toContain('发起单预计 7.00')
    panel.querySelector<HTMLElement>('[data-save-split]')!.click()
    await vi.waitFor(() => expect(mockedRequest.mock.calls.some((call) => (call[0] as ReqConfig).url?.endsWith('/split-peer/'))).toBe(true))
    const call = mockedRequest.mock.calls.map((entry) => entry[0] as ReqConfig).find((item) => item.url?.endsWith('/split-peer/'))!
    expect(call.data).toEqual({
      remaining_project_text: '发起子单项目',
      new_orders: [{ project_text: '新增项目', total_price: '5.00' }],
      source_total_price: '12.00',
      existing_orders: [{ order_no: anchorNo, project_text: '原单项目已改' }],
    })
  })
})
