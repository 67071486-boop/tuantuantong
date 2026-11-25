// @vitest-environment jsdom
/**
 * 大厅的**第三路**（上家鸡蛋铺线 Task 12）：`origin="upstream"` 的镜像行渲染 +
 * 「接手此单」真的打**本线路**的端点（`/api/business/hall/upstream/<镜像行主键>/claim/`）。
 *
 * ⚠️ 这条测试的用途正是 `AGENTS.md` §5-G 那条：`typecheck` 看不见选择器、`lint` 看不见类名，
 * 而「点了没反应 / 点错端点 / 参数带错」是**静默**的。
 *
 * ⭐ 它同时钉住 Task 11 ↔ Task 12 那个**集成缺口**的前端一半：`data-id` 必须是行里的
 * **`mirror_pk`**（⛔ 不是 `remote_order_id`：端点的 `<int:pk>` 按本表主键取行）。
 * 后端那一半由 `src/tests/test_jidanpu_upstream_hall.py::
 * test_the_row_mirror_pk_reaches_the_claim_endpoint` 钉住（行 → 参数 → 端点）。
 *
 * ⚠️ **两道门**：① 权限门（`grab_hall.take_order`，与另两路同一个节点）；② **数据门**
 * （`mirror_pk` 不是正整数 ⇒ **不画**按钮，fail-closed）。判据都在 `buildHallCols` 与
 * 挂载期那两处，本文件都钉。
 *
 * ⛔ 零真网络：`@/api/hall` / `@/api/jidanpuUpstream` / `@/api/auth` / `@/api/config`
 * 全在 import 路径上打桩，证据件走全局 `fetch` 的桩（其余 URL 一律抛）。
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { HallListItem } from '@/api/hall'

vi.mock('@/api/hall', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hall')>()
  // ⭐ 与主 spec 同款：页面抢单走 `claimOrderWithMessage`
  // （要读后端 `msg` 里的「未能撤回」警告）⇒ 两个都替身化；本文件只驱动本线路行，
  // 这里只为「另两路不被打到」给出可断言的替身（默认转接见 beforeEach）。
  return { ...actual, claimOrder: vi.fn(), claimOrderWithMessage: vi.fn() }
})

vi.mock('@/api/jidanpuUpstream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/jidanpuUpstream')>()
  // ⭐ 页面**本线路**抢单走 `claimUpstreamMirrorWithMessage`
  // （同一条请求、额外带出后端信封的 `msg`）⇒ 两个都替身化；默认让 `WithMessage`
  // 转接旧桩 `claimUpstreamMirror`（既有用例仍只配旧桩即可驱动全流程）。
  return { ...actual, claimUpstreamMirror: vi.fn(), claimUpstreamMirrorWithMessage: vi.fn() }
})

vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn() }
})

import { fetchMe, type User } from '@/api/auth'
import { claimOrder, claimOrderWithMessage } from '@/api/hall'
import { claimUpstreamMirror, claimUpstreamMirrorWithMessage } from '@/api/jidanpuUpstream'
import {
  buildHallCols,
  claimTargetOf,
  CLAIM_BUTTON_CLASS,
  CLAIM_BUTTON_TEXT,
  CLAIM_CONFIRM_TEXT,
  mountHallPage,
} from '../order-insiderecorderlist'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-insiderecorderlist.html'),
  'utf8',
)

/** 本线路镜像行主键（合成值；与 `data-id` 那条判据同源但**不是**自指——它是外部契约值）。 */
const MIRROR_PK = 7

/** 一行**本线路镜像行**的真实形状（后端 `jidanpu_upstream.serialize_upstream_row` 的键集）。 */
const UPSTREAM_ROW: HallListItem = {
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
  created_at: '2026-09-28T10:32:00+08:00',
  hall_price: '33.00',
  game_role_name: '上家合成角色',
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
  // ⭐ 镜像行没有 MAIN 绩效行 ⇒ 恒 `false`（键必须在）。
  taker_settled: false,
  origin: 'upstream',
  source_system: 'jidanpu',
  remote_order_id: '20260928000000000911',
  remote_order_view: 'VIEW-UP-911',
  remote_game_area: '安卓',
  remote_game_server: '微信区',
  remote_hours_desc: '',
  // ⭐ 本线路**唯一**非空的那一档（另两路恒 `null`）。
  mirror_pk: MIRROR_PK,
  // ⭐ D-0296四键：优选 / 灰卡 / 离厅原因 / 模拟单主键（本文件夹具全按「不是」给）。
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
    layer: {
      msg,
      open: vi.fn(),
      close: vi.fn(),
      confirm: vi.fn(),
      prompt: vi.fn(),
      tips: vi.fn(),
      // ⭐ 与主 spec 同款：抢单成功的「未能撤回」警告走 `layer.alert`
      // （停留更久、要人工确认），⛔ 不一闪而过。
      alert: vi.fn(),
    },
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

// `canViewShots`：本文件测的是镜像行渲染（截图那列不在此列）⇒ 给它「有节点」，
// 让其余列的断言不受权限门影响（「参考图」对镜像行另有数据门，见主 spec）。
const OPTS = {
  gameName: () => '测试游戏甲',
  serverName: () => '',
  canViewShots: true,
}

beforeEach(() => {
  LAYUI.msg.mockClear()
  LAYUI.layer.confirm.mockClear()
  LAYUI.layer.close.mockClear()
  LAYUI.layer.alert.mockClear()
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.formHandlers.clear()
  vi.mocked(claimOrder).mockReset()
  vi.mocked(claimOrder).mockResolvedValue({} as never)
  // ⭐ 与主 spec 同款：默认「后端 msg 为空、抢单成功」——
  // `WithMessage` 转接旧桩，既有用例仍只配旧桩（`claimOrder` / `claimUpstreamMirror`）
  // 即可驱动全流程；要测「msg 带警告」的用例在自己体内覆写 `WithMessage`。
  vi.mocked(claimOrderWithMessage).mockReset()
  vi.mocked(claimOrderWithMessage).mockImplementation(
    (async (orderNo: string) => ({
      order: await vi.mocked(claimOrder)(orderNo),
      message: '',
    })) as never,
  )
  vi.mocked(claimUpstreamMirror).mockReset()
  vi.mocked(claimUpstreamMirror).mockResolvedValue({} as never)
  vi.mocked(claimUpstreamMirrorWithMessage).mockReset()
  vi.mocked(claimUpstreamMirrorWithMessage).mockImplementation(
    (async (mirrorPk: number) => ({
      order: await vi.mocked(claimUpstreamMirror)(mirrorPk),
      message: '',
    })) as never,
  )
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

describe('本线路镜像行的渲染（与另两路同一张表）', () => {
  it('订单号列显示远端展示单号（本线路的 `order_no` 也是空串）', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    const html = colCell(cols, 'orderIdView')(UPSTREAM_ROW)
    expect(html).toContain('VIEW-UP-911')
    expect(html).not.toContain('data-orderIdView=""')
  })

  it('角色信息列用远端文本区服（`server` 是 null ⇒ ⛔ 不查我方区服字典）', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    expect(colCell(cols, 'gameInfo')(UPSTREAM_ROW)).toBe(
      '<span title="">[测试游戏甲]安卓微信区</span>',
    )
  })

  it('抢单按钮的 `data-id` 是**镜像行主键**、`data-origin="upstream"`（⛔ 不是上家单号）', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    const html = colCell(cols, 'actioncol')(UPSTREAM_ROW)
    expect(html).toContain(CLAIM_BUTTON_CLASS)
    expect(html).toContain(CLAIM_BUTTON_TEXT)
    expect(html).toContain('data-origin="upstream"')
    expect(html).toContain('data-source="jidanpu"')
    expect(html).toContain(`data-id="${MIRROR_PK}"`)
    // ⛔ 承重判据：**不许**把上家单号当主键（那条请求会打不中那一行）。
    expect(html).not.toContain(`data-id="${UPSTREAM_ROW.remote_order_id}"`)
  })
})

describe('本线路的两道门', () => {
  it('没持 grab_hall.take_order ⇒ 两列都不画按钮', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: false })
    for (const field of ['actioncol', 'actioncol2']) {
      expect(colCell(cols, field)(UPSTREAM_ROW)).toBe('')
    }
  })

  it('有节点但 `mirror_pk` 不是正整数 ⇒ 也是**不画**（fail-closed，⛔ 不拿空值拼 URL）', () => {
    const cols = buildHallCols({ ...OPTS, canClaim: true })
    for (const field of ['actioncol', 'actioncol2']) {
      const cell = colCell(cols, field)
      expect(cell({ ...UPSTREAM_ROW, mirror_pk: null })).toBe('')
      expect(cell({ ...UPSTREAM_ROW, mirror_pk: 0 })).toBe('')
      expect(cell({ ...UPSTREAM_ROW, mirror_pk: -3 })).toBe('')
      expect(cell({ ...UPSTREAM_ROW, mirror_pk: 1.5 })).toBe('')
      expect(cell({ ...UPSTREAM_ROW, mirror_pk: Number.NaN })).toBe('')
    }
    // 阳性对照：正整数那一档**画**（否则上一行可能是恒真的空断言）。
    expect(colCell(cols, 'actioncol')(UPSTREAM_ROW)).toContain(CLAIM_BUTTON_CLASS)
  })

  it('`claimTargetOf` 各源的目标（渲染期与点击期**共用**这一处判据）', () => {
    expect(claimTargetOf(UPSTREAM_ROW)).toBe(String(MIRROR_PK))
    expect(claimTargetOf({ ...UPSTREAM_ROW, mirror_pk: null })).toBeNull()
    // ⭐ L5：`remote` 已拆除 ⇒ 即使载荷里还出现这个 origin，也**不画按钮**、
    // 不会拿 `remote_order_id` 去打任何接单端点（fail-closed 的一半）。
    expect(claimTargetOf({ ...UPSTREAM_ROW, origin: 'remote', mirror_pk: null })).toBeNull()
    expect(
      claimTargetOf({ ...UPSTREAM_ROW, origin: 'native', mirror_pk: null, order_no: '20260928000000000001' }),
    ).toBe('20260928000000000001')
  })

  it('挂载期按 fetchMe 的节点算：无节点 ⇒ table.render 的列里也没有按钮', async () => {
    vi.mocked(fetchMe).mockResolvedValue(NO_CLAIM_ME)
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
    expect(vi.mocked(fetchMe)).toHaveBeenCalled()

    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    const cols = (cfg.cols as Record<string, unknown>[][])[0]!
    expect(colCell(cols, 'actioncol')(UPSTREAM_ROW)).toBe('')
    cleanup()
  })
})

describe('本线路接单：真的打本线端点（驱动真交互）', () => {
  it('点「接手此单」→ confirm → claimUpstreamMirrorWithMessage(镜像行主键)，⛔ 不碰另两路', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })

    // 按钮用**挂载期那一份**列 templet 渲染（默认夹具持字面节点 ⇒ 两道门都放行）：
    // 门 → 标记 → 点击委托 → 端点，整条链一次判完。
    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    const cols = (cfg.cols as Record<string, unknown>[][])[0]!
    const box = document.createElement('div')
    box.innerHTML = colCell(cols, 'actioncol')(UPSTREAM_ROW)
    root.appendChild(box.firstElementChild!)

    root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.confirm.mock.calls[0]![0]).toBe(CLAIM_CONFIRM_TEXT)
    const yes = LAYUI.layer.confirm.mock.calls[0]![1] as (index: number) => void
    yes(0)
    // ⭐ 页面走 `claimUpstreamMirrorWithMessage`（旧 `claimUpstreamMirror`
    // 已不再被页面 import）。参数是**主键数字 7**（⛔ 不是 `remote_order_id` 那串 20 位数字）。
    expect(vi.mocked(claimUpstreamMirrorWithMessage)).toHaveBeenCalledWith(MIRROR_PK)
    expect(vi.mocked(claimOrderWithMessage)).not.toHaveBeenCalled()
    await Promise.resolve()
    cleanup()
  })

  it('`data-id` 被人为改成非正整数 ⇒ 一次请求都不发（点击期那道形状闸）', async () => {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    const cols = (cfg.cols as Record<string, unknown>[][])[0]!
    const box = document.createElement('div')
    box.innerHTML = colCell(cols, 'actioncol')(UPSTREAM_ROW)
    root.appendChild(box.firstElementChild!)
    // 人为篡改 DOM（真人做不到，但这正是「形状先行拒绝」要防的那一档）。
    root.querySelector<HTMLElement>('.recThisOrder')!.dataset.id = 'null'
    root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.layer.confirm).not.toHaveBeenCalled()
    expect(vi.mocked(claimUpstreamMirrorWithMessage)).not.toHaveBeenCalled()
    cleanup()
  })

  it('接单成功：**空 msg** → 回退 toast「抢单成功」（⛔ 没有任何按回包字段变形的分支）', async () => {
    // ⚠️ 业务方 2026-09-29 口径变更：**不要**那套「请填发单价」告示（改成「上家没给价按 0 算」，
    //    后端已把那个缺价标记整个下线）⇒ 前端**一次都不读**那个键（本文件也⛔ 不保留它的字面量）。
    // ⭐ 回包是 `{ order, message }`，`order` 换成任何形状提示都不变
    //    （`message` 为空串才回退默认文案「抢单成功」，钉 `time: 1500` 与主 spec 同款）。
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
    for (const reply of [
      {}, // 空的 order（字段全部缺席 = 最贴近「键已被删掉」那一档）
      { order_no: '20260928000000000001', hall_price: null },
    ]) {
      LAYUI.msg.mockClear()
      LAYUI.layer.confirm.mockClear()
      vi.mocked(claimUpstreamMirror).mockReset()
      vi.mocked(claimUpstreamMirror).mockResolvedValue(reply as never)
      root.querySelector('.recThisOrder')?.remove()
      const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
      const cols = (cfg.cols as Record<string, unknown>[][])[0]!
      const box = document.createElement('div')
      box.innerHTML = colCell(cols, 'actioncol')(UPSTREAM_ROW)
      root.appendChild(box.firstElementChild!)
      root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const yes = LAYUI.layer.confirm.mock.calls[0]![1] as (index: number) => void
      yes(0)
      await Promise.resolve()
      await Promise.resolve()
      expect(LAYUI.msg).toHaveBeenCalledWith('抢单成功', { time: 1500 })
    }
    cleanup()
  })

  it('本线路抢单成功但后端 msg 带「未能撤回」警告 ⇒ **原文**用 layer.alert + 刷新（⛔ 不吞）', async () => {
    // ⭐ 与主 spec 同款形态：警告只在信封 `msg` 里，接口仍是成功码
    // ⇒ 必须原文 `layer.alert`（人工确认，⛔ 不一闪而过），且**不**再叠默认「抢单成功」toast。
    const WARNING =
      '代练通订单未能撤回（原话：可操作资金不足，不能发布.），' +
      '请先到「第三方平台订单管理」撤回代练通平台订单'
    vi.mocked(claimUpstreamMirrorWithMessage).mockResolvedValue({
      order: {} as never,
      message: WARNING,
    } as never)
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
    const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
    const cols = (cfg.cols as Record<string, unknown>[][])[0]!
    const box = document.createElement('div')
    box.innerHTML = colCell(cols, 'actioncol')(UPSTREAM_ROW)
    root.appendChild(box.firstElementChild!)
    root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const yes = LAYUI.layer.confirm.mock.calls[0]![1] as (index: number) => void
    yes(0)
    await vi.waitFor(() => expect(LAYUI.layer.alert).toHaveBeenCalled())
    expect(LAYUI.layer.alert).toHaveBeenCalledWith(
      WARNING,
      expect.objectContaining({ title: '注意：代练通订单未能撤回' }),
    )
    expect(LAYUI.msg).not.toHaveBeenCalledWith('抢单成功', { time: 1500 })
    expect(LAYUI.table.reload).toHaveBeenCalled()
    cleanup()
  })
})
