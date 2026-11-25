// @vitest-environment jsdom
/**
 * 大厅的**团团通上家那一侧**（`origin="peer"`，D-0303 / 大厅镜像 P2）：
 * 行的渲染 + 「接手此单」真的打 `POST /api/business/hall/peer/<mirror_pk>/claim/`，
 * 并按**业务码**给结局（成功受理 / 已被接走 / 其余原样）。
 *
 * ⚠️ 这条测试的用途正是 `AGENTS.md` §5-G 那条：`typecheck` 看不见选择器、`lint` 看不见类名，
 * 而「点了没反应 / 点错端点 / 参数带错」是**静默**的。
 *
 * 钉住的五件事（对应 P2 任务书 §1–§3）：
 *  ① 行照大厅既有样式渲染（单号取读口的**上家单号键** `remote_order_view || remote_order_id`，
 *     ⛔ 不是空的我方 `order_no`；区服 / 项目 / 展示价 / 时间都在）；
 *  ② 接单打**本线**端点、参数是**镜像行主键 `mirror_pk`**（⛔ 不是上家单号——本线的 `<int:pk>`
 *     按 `PeerHallMirror` 的主键取行，与鸡蛋铺那条「按本表主键」同形但端点不同）；
 *  ③ 成功「正在受理中」+ **置灰不删卡、不刷新**；`1012` **刷新**；其余原样显示后端 `msg`、卡保留；
 *  ④ 双点只发一次（在途集合按 `mirror_pk`，点击期 + 确认回调各判一次）；
 *  ⑤ `mirror_pk` 缺 / 0 / 非正整数 ⇒ **一颗按钮都不画**、一次请求都不发（fail-closed）。
 *
 * ⛔ 零真网络：`@/api/hall` 的接单函数、`@/api/auth`、`@/api/config` 全在 import 路径上打桩；
 * 证据件走全局 `fetch` 的桩。`claimPeerMirror` 的 **URL 形状**另用 `importActual` 走真模块
 * + 替身 `@/api/http` 钉一次（页面自造的中间态不算证据）。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { HallListItem } from '@/api/hall'

vi.mock('@/api/http', () => ({ request: vi.fn(), http: {} }))

vi.mock('@/api/hall', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hall')>()
  return {
    ...actual,
    claimOrder: vi.fn(),
    claimPeerMirror: vi.fn(),
  }
})

vi.mock('@/api/jidanpuUpstream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/jidanpuUpstream')>()
  return { ...actual, claimUpstreamMirror: vi.fn() }
})

vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn() }
})

import { request } from '@/api/http'
import { fetchMe, type User } from '@/api/auth'
import {
  claimOrder,
  claimPeerMirror,
  PEER_CLAIM_ACCEPTED_TEXT,
} from '@/api/hall'
import { BizError } from '@/api/errors'
import { claimUpstreamMirror } from '@/api/jidanpuUpstream'
import {
  buildHallCols,
  claimTargetOf,
  CLAIM_BUTTON_CLASS,
  CLAIM_BUTTON_SELECTOR,
  CLAIM_BUTTON_TEXT,
  CLAIM_CONFIRM_TEXT,
  HALL_TABLE_ID,
  mountHallPage,
  PEER_CLAIMED_ROW_ATTR,
} from '../order-insiderecorderlist'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-insiderecorderlist.html'),
  'utf8',
)

/**
 * 镜像行主键（合成值；接单端点的路径参数就是它）。
 *
 * ⚠️ 刻意与下面 `REMOTE_ID`（上家单号的形状）**不同**：那条「⛔ 不许拿上家单号当主键」
 * 的判据要能分辨这两个值，否则断言恒真。
 */
const MIRROR_PK = 7
/** 上家单号（读口 `remote_order_id` / `remote_order_view`；合成值）。 */
const REMOTE_ID = '20261025000000001111'

/**
 * 一行**团团通上家**镜像行（后端 `peer_hall.serialize_peer_row` 的投影形状）。
 *
 * ⚠️ `mirror_pk` 在这条线里**非空**（= `PeerHallMirror.pk`）；`remote_game_area` /
 * `remote_game_server` 恒空串（团团通载荷里没有区服文本，spec §3.1 的列清单如此），
 * `source_system` = `"peer"`（`peer_hall.PEER_SOURCE_SYSTEM`）。
 */
const PEER_ROW: HallListItem = {
  order_no: '',
  source_order_no: '',
  source: '',
  taker_name: '',
  status: 'WAITING_GRAB',
  game: 3,
  server: null,
  project_text: '上家合成项目',
  total_price: '0.00',
  refund_fee: '0.00',
  customer_phone: '',
  is_urgent: false,
  created_at: '2026-10-25T10:32:00+08:00',
  hall_price: '72.00',
  game_role_name: '',
  customer_ww: '',
  sales_agent: '',
  created_by: '',
  remark: '上家合成备注',
  net_received: '0.00',
  hall_publisher_name: '',
  completed_at: null,
  time_requirement: '',
  payment_status: '',
  payment_method: '',
  taker_settled: false,
  origin: 'peer',
  source_system: 'peer',
  remote_order_id: REMOTE_ID,
  remote_order_view: REMOTE_ID,
  remote_game_area: '',
  remote_game_server: '',
  remote_hours_desc: '',
  mirror_pk: MIRROR_PK,
  is_preferred: false,
  is_exited: false,
  exit_kind: null,
  sim_pk: null,
}

/** 持节点 `grab_hall.take_order` 的打手（非管理员 ⇒ 只能靠 `menu_permissions`）。 */
const TAKER_ME: User = {
  id: 7,
  username: 'taker-a',
  display_name: '打手甲',
  identity_plane: 'tenant',
  is_tenant_admin: false,
  is_account_admin: false,
  roles: ['代练员'],
  permissions: [],
  // 字面值（E-0024）：把常量喂回夹具会让「常量本身写错了」无从发现。
  menu_permissions: ['grab_hall.take_order'],
  game_ids: [3],
}

/** 只有栏目 `grab_hall`、**没有** `grab_hall.take_order` 的人（画了就会 403 的那一类）。 */
const NO_CLAIM_ME: User = { ...TAKER_ME, menu_permissions: ['grab_hall'] }

const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    msg,
    formHandlers,
    formSelects: { value: vi.fn(() => []), data: vi.fn(), on: vi.fn() },
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: { render: vi.fn(), reload: vi.fn(), on: vi.fn() },
    layer: { msg, open: vi.fn(), close: vi.fn(), confirm: vi.fn(), prompt: vi.fn(), tips: vi.fn() },
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

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return {
    ...actual,
    fetchGames: vi.fn(async () => [
      { id: 3, name: '测试游戏甲', sort_order: 1, is_active: true, created_at: '' },
    ]),
    fetchServers: vi.fn(async () => []),
  }
})

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** `buildHallCols` 的一个 templet（按 `field` 取）。 */
function colCell(
  cols: Record<string, unknown>[],
  field: string,
): (row: HallListItem) => string {
  const col = cols.find((c) => (c as { field?: string }).field === field) as {
    templet: (row: HallListItem) => string
  }
  expect(col, `列 ${field} 不存在`).toBeTruthy()
  return col.templet
}

// `canViewShots`：本文件测的是镜像行渲染 / 接单（截图那列不在此列）⇒ 给它「有节点」，
// 让其余列的断言不受权限门影响（「参考图」对镜像行另有数据门，见主 spec）。
const OPTS = {
  gameName: () => '测试游戏甲',
  serverName: () => '',
  canViewShots: true,
}

/** 挂载整页并取出**挂载期那一份**列（权限门已过）。 */
async function mountWithCols(): Promise<{
  root: HTMLElement
  cleanup: () => void
  cols: Record<string, unknown>[]
}> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
  const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
  const cols = (cfg.cols as Record<string, unknown>[][])[0]!
  return { root, cleanup, cols }
}

/** 把这一行渲染成一颗**在 `<tr>` 里**的按钮（置灰判据要打在行上）。 */
function appendRowButton(root: HTMLElement, html: string): HTMLElement {
  const table = document.createElement('table')
  const tbody = document.createElement('tbody')
  const tr = document.createElement('tr')
  const td = document.createElement('td')
  td.innerHTML = html
  tr.appendChild(td)
  tbody.appendChild(tr)
  table.appendChild(tbody)
  root.appendChild(table)
  return td.querySelector<HTMLElement>(CLAIM_BUTTON_SELECTOR)!
}

/** 渲染一行（两颗按钮都在同一个 `<tr>` 里）并返回那一行。 */
function appendRowWithBothButtons(
  root: HTMLElement,
  cols: Record<string, unknown>[],
): HTMLElement {
  const td = document.createElement('td')
  td.innerHTML = colCell(cols, 'actioncol')(PEER_ROW) + colCell(cols, 'actioncol2')(PEER_ROW)
  const tr = document.createElement('tr')
  tr.appendChild(td)
  const tbody = document.createElement('tbody')
  tbody.appendChild(tr)
  const table = document.createElement('table')
  table.appendChild(tbody)
  root.appendChild(table)
  return tr
}

function click(el: HTMLElement): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

/** 点确认框里的「确定」（第 i 个）。 */
function acceptConfirm(index = 0): void {
  const call = LAYUI.layer.confirm.mock.calls[index]
  expect(call, `没有第 ${index + 1} 个确认框`).toBeTruthy()
  const yes = call![1] as (index: number) => void
  yes(index)
}

async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

beforeEach(() => {
  LAYUI.msg.mockClear()
  LAYUI.layer.confirm.mockReset()
  LAYUI.layer.close.mockClear()
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.formHandlers.clear()
  vi.mocked(claimOrder).mockReset()
  vi.mocked(claimOrder).mockResolvedValue({} as never)
  vi.mocked(claimPeerMirror).mockReset()
  vi.mocked(claimPeerMirror).mockResolvedValue({
    mirror_pk: MIRROR_PK,
    peer_order_no: REMOTE_ID,
    status: 'claiming',
    status_text: '正在接单',
  })
  vi.mocked(claimUpstreamMirror).mockReset()
  vi.mocked(claimUpstreamMirror).mockResolvedValue({} as never)
  vi.mocked(request).mockReset()
  vi.mocked(request).mockResolvedValue({} as never)
  vi.mocked(fetchMe).mockReset()
  vi.mocked(fetchMe).mockResolvedValue(TAKER_ME)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-insiderecorderlist.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('peer 行的卡片渲染（与另几路同一张表）', () => {
  it('订单号用**上家单号键**（`order_no` 是空串）、区服 / 项目 / 展示价 / 时间都在', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    const orderCell = colCell(cols, 'orderIdView')(PEER_ROW)
    expect(orderCell).toContain(REMOTE_ID)
    // ⛔ 承重判据：镜像行没有我方单号，不许渲染成空壳。
    expect(orderCell).not.toContain('data-orderIdView=""')
    // 项目 / 区服：团团通载荷没有区服文本（`remote_game_*` 恒空串）⇒ 只出 `[游戏名]`，
    // ⛔ 不许把空的远端文本渲染成 `null` / 我方区服主键。
    expect(colCell(cols, 'project')(PEER_ROW)).toBe('上家合成项目')
    expect(colCell(cols, 'gameInfo')(PEER_ROW)).toBe('<span title="">[测试游戏甲]</span>')
    // 展示价：上家发单价原样（v1 不打折）。
    expect(colCell(cols, 'recFee')(PEER_ROW)).toContain('72.00')
    // 时间：进厅时刻（`first_seen_at` → `created_at`）。
    expect(colCell(cols, 'recOrderTime')(PEER_ROW)).toContain('2026-10-25 10:32')
  })

  it('接单按钮：`data-id` 是**镜像行主键**、`data-origin="peer"`、`data-source="peer"`', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    const html = colCell(cols, 'actioncol')(PEER_ROW)
    expect(html).toContain(CLAIM_BUTTON_CLASS)
    expect(html).toContain(CLAIM_BUTTON_TEXT)
    expect(html).toContain('data-origin="peer"')
    expect(html).toContain('data-source="peer"')
    expect(html).toContain(`data-id="${MIRROR_PK}"`)
    // ⛔ 承重判据：**不许**把上家单号当主键（那条请求会按 `pk` 解析失败）。
    expect(html).not.toContain(`data-id="${REMOTE_ID}"`)
  })

  it('`claimTargetOf` 的 peer 分支：`mirror_pk` 正整数 ⇒ 它的十进制串；否则 null', () => {
    expect(claimTargetOf(PEER_ROW)).toBe(String(MIRROR_PK))
    // 原生行仍按我方单号（peer 那一支不许把它吃掉）。
    expect(
      claimTargetOf({ ...PEER_ROW, origin: 'native', order_no: '20261025000000000001' }),
    ).toBe('20261025000000000001')
    // 鸡蛋铺镜像行仍按**它自己**的 mirror_pk（同一个值也照旧 ⇒ 两条线各走各的端点）。
    expect(claimTargetOf({ ...PEER_ROW, origin: 'upstream' })).toBe(String(MIRROR_PK))
  })
})

describe('peer 行的数据门（fail-closed）', () => {
  it('`mirror_pk` 缺 / 0 / 负数 / 小数 / NaN ⇒ 两列都**不画**按钮', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    for (const field of ['actioncol', 'actioncol2']) {
      const cell = colCell(cols, field)
      for (const bad of [null, 0, -3, 1.5, Number.NaN, undefined]) {
        expect(cell({ ...PEER_ROW, mirror_pk: bad as number | null })).toBe('')
      }
    }
    // 阳性对照：正整数那一档**画**（否则上面可能是恒真的空断言）。
    expect(colCell(cols, 'actioncol')(PEER_ROW)).toContain(CLAIM_BUTTON_CLASS)
  })

  it('`claimTargetOf` 同样收窄（渲染期与点击期**共用**这一处判据）', () => {
    for (const bad of [null, 0, -3, 1.5, Number.NaN]) {
      expect(claimTargetOf({ ...PEER_ROW, mirror_pk: bad as number | null })).toBeNull()
    }
  })

  it('没持 `grab_hall.take_order` ⇒ 两列都不画（权限门，不是数据门）', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: false })
    for (const field of ['actioncol', 'actioncol2']) {
      expect(colCell(cols, field)(PEER_ROW)).toBe('')
    }
  })

  it('挂载期按 fetchMe 的节点算：无节点 ⇒ table.render 的列里也没有按钮', async () => {
    vi.mocked(fetchMe).mockResolvedValue(NO_CLAIM_ME)
    const { root, cleanup } = await mountWithCols()
    expect(vi.mocked(fetchMe)).toHaveBeenCalled()
    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    const cols = (cfg.cols as Record<string, unknown>[][])[0]!
    expect(colCell(cols, 'actioncol')(PEER_ROW)).toBe('')
    cleanup()
    void root
  })
})

describe('peer 接单：真的打本线端点（驱动真交互）', () => {
  it('点「接手此单」→ confirm → claimPeerMirror(镜像行主键)，⛔ 不碰另三条入口', async () => {
    const { root, cleanup, cols } = await mountWithCols()
    const button = appendRowButton(root, colCell(cols, 'actioncol')(PEER_ROW))
    click(button)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.confirm.mock.calls[0]![0]).toBe(CLAIM_CONFIRM_TEXT)
    acceptConfirm()
    expect(vi.mocked(claimPeerMirror)).toHaveBeenCalledWith(MIRROR_PK)
    expect(vi.mocked(claimOrder)).not.toHaveBeenCalled()
    expect(vi.mocked(claimUpstreamMirror)).not.toHaveBeenCalled()
    await flush()
    cleanup()
  })

  it('成功 ⇒ 「正在受理中」+ 卡片**置灰**（两颗按钮都灰、再点零请求）；⛔ 不刷新', async () => {
    const { root, cleanup, cols } = await mountWithCols()
    const tr = appendRowWithBothButtons(root, cols)

    click(tr.querySelector<HTMLElement>(CLAIM_BUTTON_SELECTOR)!)
    acceptConfirm()
    await flush()

    expect(LAYUI.msg).toHaveBeenCalledWith(PEER_CLAIM_ACCEPTED_TEXT, { time: 1500 })
    expect(LAYUI.msg).not.toHaveBeenCalledWith('抢单成功', expect.anything())
    expect(tr.getAttribute(PEER_CLAIMED_ROW_ATTR)).toBe('1')
    const buttons = Array.from(tr.querySelectorAll<HTMLElement>(CLAIM_BUTTON_SELECTOR))
    expect(buttons).toHaveLength(2)
    for (const b of buttons) {
      expect(b.classList.contains('layui-btn-disabled')).toBe(true)
      expect(b.getAttribute('aria-disabled')).toBe('true')
    }
    // ⛔ 不删卡 / 不刷新（确认制：刷了这张卡会从 `listed` 投影里直接消失）。
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    // 置灰之后再点：连确认框都不该再弹、更不该再发请求。
    LAYUI.layer.confirm.mockClear()
    click(buttons[0]!)
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    expect(vi.mocked(claimPeerMirror)).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('双点（两个确认框都点「确定」）只发一次：确认回调里那道闸**承重**', async () => {
    let release: (value: unknown) => void = () => undefined
    vi.mocked(claimPeerMirror).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve
        }) as never,
    )
    const { root, cleanup, cols } = await mountWithCols()
    const button = appendRowButton(root, colCell(cols, 'actioncol')(PEER_ROW))

    // 双击：两个 click 都在「第一发」之前到达 ⇒ 会弹两个确认框。
    click(button)
    click(button)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(2)
    // 两个确认框都点「确定」⇒ 第二个回调必须撞在途集合（⛔ 否则双击 = 两发请求）。
    acceptConfirm(0)
    acceptConfirm(1)
    expect(vi.mocked(claimPeerMirror)).toHaveBeenCalledTimes(1)
    release({})
    await flush()
    cleanup()
  })

  it('第一发在途时再点 ⇒ 连确认框都不弹（点击期那道闸）、请求仍是一发', async () => {
    let release: (value: unknown) => void = () => undefined
    vi.mocked(claimPeerMirror).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve
        }) as never,
    )
    const { root, cleanup, cols } = await mountWithCols()
    const button = appendRowButton(root, colCell(cols, 'actioncol')(PEER_ROW))

    click(button)
    acceptConfirm()
    click(button)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(vi.mocked(claimPeerMirror)).toHaveBeenCalledTimes(1)
    release({})
    await flush()
    cleanup()
  })

  it('`data-id` 被人为改成非正整数 ⇒ 一次请求都不发（点击期那道形状闸）', async () => {
    const { root, cleanup, cols } = await mountWithCols()
    const button = appendRowButton(root, colCell(cols, 'actioncol')(PEER_ROW))
    button.dataset.id = '0'
    click(button)
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    expect(vi.mocked(claimPeerMirror)).not.toHaveBeenCalled()
    cleanup()
  })
})

describe('peer 接单的结局映射（业务码 → 提示 / 刷新 / 置灰）', () => {
  async function claimAndGet(): Promise<{ cleanup: () => void; tr: HTMLElement }> {
    const { root, cleanup, cols } = await mountWithCols()
    const tr = appendRowWithBothButtons(root, cols)
    click(tr.querySelector<HTMLElement>(CLAIM_BUTTON_SELECTOR)!)
    acceptConfirm()
    await flush()
    return { cleanup, tr }
  }

  it('`1012`（已被接走 / 正在处理中）⇒ 原样显示后端 msg + **刷新**；⛔ 不置灰', async () => {
    vi.mocked(claimPeerMirror).mockRejectedValue(
      new BizError(1012, '该笔订单已被其他打手接取'),
    )
    const { cleanup, tr } = await claimAndGet()
    expect(LAYUI.msg).toHaveBeenCalledWith('该笔订单已被其他打手接取', { time: 1500 })
    expect(LAYUI.table.reload).toHaveBeenCalledWith(HALL_TABLE_ID, expect.anything())
    // 这一张的状态交给重取后的载荷决定 ⇒ 本地**不**置灰。
    expect(tr.getAttribute(PEER_CLAIMED_ROW_ATTR)).toBeNull()
    expect(tr.style.opacity).toBe('')
    cleanup()
  })

  it('`1015`（上家没开同步 / 绑定停了）⇒ 原样显示后端 msg、卡保留、⛔ 不刷新', async () => {
    vi.mocked(claimPeerMirror).mockRejectedValue(
      new BizError(1015, '还没配置好鸡蛋铺同步账号，请联系管理员'),
    )
    const { cleanup, tr } = await claimAndGet()
    expect(LAYUI.msg).toHaveBeenCalledWith('还没配置好鸡蛋铺同步账号，请联系管理员', {
      time: 1500,
    })
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(tr.isConnected).toBe(true)
    expect(tr.getAttribute(PEER_CLAIMED_ROW_ATTR)).toBeNull()
    const button = tr.querySelector<HTMLElement>(CLAIM_BUTTON_SELECTOR)!
    expect(button.classList.contains('layui-btn-disabled')).toBe(false)
    cleanup()
  })

  it('其它错误（403 / 网络）⇒ 原样显示、卡保留（⛔ 不换成任何自拟文案）', async () => {
    vi.mocked(claimPeerMirror).mockRejectedValue(new Error('你没有权限执行该操作'))
    const { cleanup, tr } = await claimAndGet()
    expect(LAYUI.msg).toHaveBeenCalledWith('你没有权限执行该操作', { time: 1500 })
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    expect(tr.isConnected).toBe(true)
    cleanup()
  })
})

describe('api 出口：URL 与参数形状（走真模块，⛔ 不用页面中间态当证据）', () => {
  it('`claimPeerMirror` ⇒ POST /api/business/hall/peer/<mirror_pk>/claim/、**没有请求体**', async () => {
    const actual = await vi.importActual<typeof import('@/api/hall')>('@/api/hall')
    await actual.claimPeerMirror(MIRROR_PK)
    expect(vi.mocked(request)).toHaveBeenCalledWith({
      method: 'post',
      url: `/api/business/hall/peer/${MIRROR_PK}/claim/`,
    })
  })

  it('主键非正整数 ⇒ 形状先行拒绝，一次请求都不发', async () => {
    const actual = await vi.importActual<typeof import('@/api/hall')>('@/api/hall')
    for (const bad of [0, -1, 1.5, Number.NaN]) {
      await expect(actual.claimPeerMirror(bad)).rejects.toThrow('镜像行主键不合法')
    }
    expect(vi.mocked(request)).not.toHaveBeenCalled()
  })
})
