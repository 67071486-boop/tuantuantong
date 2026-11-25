// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { HallListItem } from '@/api/hall'
import { toHallParams } from '@/api/hall'
import {
  buildHallCols,
  CLAIM_BUTTON_CLASS,
  CLAIM_BUTTON_TEXT,
  CLAIM_CONFIRM_TEXT,
  CLAIM_NODE,
  COLS,
  mountHallPage,
  renderHallTotal,
  SCREENSHOT_VIEW_NODE,
  SHOT_BUTTON_CLASS,
  TABLE,
  toHallSearch,
} from '../order-insiderecorderlist'

/**
 * 抢单要打桩 `claimOrder`（此前 `@/api/hall` 是**真模块**：任务 2 只用它的 `toHallParams`）。
 * 其余出口（`toHallParams` / 类型）保持真值——`importOriginal` 保真，只换这一个函数。
 *
 * `vi.mock` 会被 vitest 提升，所以下面两行 `import` 写在它之后（与 `orders.spec.ts` 同形）。
 */
vi.mock('@/api/hall', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hall')>()
  // ⚠️ 只换网络出口，其余用 `importOriginal` 保真。
  // ⭐ 页面抢单走 `claimOrderWithMessage`（要读后端 `msg` 里的「未能撤回」警告）
  // ⇒ 两个都替身化；默认让 `WithMessage` 转接 `claimOrder`（既有用例仍只配 `claimOrder`）。
  return { ...actual, claimOrder: vi.fn(), claimOrderWithMessage: vi.fn() }
})

import { claimOrder, claimOrderWithMessage } from '@/api/hall'
import { fetchServers } from '@/api/config'
import { BizError, HttpError, NetworkError } from '@/api/errors'
import { fetchScreenshots, type ScreenshotItem } from '@/api/screenshots'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/order-insiderecorderlist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-insiderecorderlist.html'),
  'utf8',
)

/** 一行真实形状的大厅载荷（字段集 = `OrderListSerializer` + `hall_price`）。 */
const ROW: HallListItem = {
  order_no: '20260920213215233029',
  source_order_no: '',
  // 订单来源的名字快照（列表载荷新增；大厅继承 `OrderListSerializer` 的字段集）。
  source: '',
  // 接单人名字（列表载荷新增；大厅列表继承同一序列化器 ⇒ 也带它，池内恒空串）。
  taker_name: '',
  status: 'WAITING_GRAB',
  game: 2,
  server: 7,
  project_text: '深渊满星',
  total_price: '1.00',
  // 累计退款额（列表载荷新增；大厅列表继承 `OrderListSerializer` 的字段集 ⇒ 也带它）。
  refund_fee: '0.00',
  customer_phone: '',
  is_urgent: false,
  created_at: '2026-09-20T21:32:15+08:00',
  hall_price: '100.00',
  // 第二批列给 `OrderListSerializer` 补的 9 个只读键——大厅继承同一份 `fields`
  // ⇒ 这里也按**真实契约**带上（缺一个，`HallListItem` 的类型就不成立）。
  game_role_name: '',
  customer_ww: '',
  sales_agent: '',
  created_by: '',
  remark: '',
  net_received: '1.00',
  hall_publisher_name: '',
  completed_at: null,
  // 第 10 个键（Task 3 执行期补进 `OrderListSerializer.Meta.fields`）：管理订单页「时间」列要读它，
  // 大厅继承同一份 `fields` ⇒ 也带它。
  time_requirement: '',
  // T7 补进列表载荷的两个键（收款状态码 `payment_status` / 自由文本 `payment_method`）：
  // 大厅继承同一份 `fields` ⇒ 按**真实契约**也带它们（缺一个 `HallListItem` 类型就不成立）。
  payment_status: 'UNSETTLED',
  payment_method: '',
  // ⭐ 大厅行没有已结账的打手绩效 ⇒ `false`（键必须在）。
  taker_settled: false,
  // ⭐ P2-A/P2-B：三源合并的加法键（原生行的取值：`origin='native'`、其余空串 / `null`）。
  // ⚠️ `HallListItem` 变宽之后，这里少一个键 `vue-tsc` 就报缺属性（`vitest` **不会**报
  //    ——「vitest 绿」证明不了类型没问题）。
  origin: 'native',
  source_system: '',
  remote_order_id: '',
  remote_order_view: '',
  remote_game_area: '',
  remote_game_server: '',
  remote_hours_desc: '',
  // ⭐ 上家鸡蛋铺线（客服个人绑定）第三路的镜像行主键（Task 12 集成补丁）：**原生行恒 `null`**。
  mirror_pk: null,
  // ⭐ D-0296四键：优选 / 灰卡 / 离厅原因 / 模拟单主键（本文件夹具全按「不是」给）。
  is_preferred: false,
  is_exited: false,
  exit_kind: null,
  sim_pk: null,
}

/**
 * 两张**已上传**的截图（形状 = 后端 `ScreenshotCollectionView.list_items` 的 item，逐键一致）。
 *
 * 第二张的 `original_name` 故意带 `<` / `>` / `"`：`layer.photos` 会把它拼进 `<img alt="…">`
 * 的属性（vendored layui，读码偏移 ~192971）⇒ 这条同时钉「说明文字」与「转义」。
 */
const SHOTS: ScreenshotItem[] = [
  {
    id: 11,
    img_type: 0,
    url: 'https://cos.example/shot-1.png?sign=abc',
    original_name: '进度1.png',
    size_bytes: 1024,
    remark: '第一关',
    uploaded_by: 7,
    created_at: '2026-10-23T10:00:00+08:00',
  },
  {
    id: 12,
    img_type: 0,
    url: 'https://cos.example/shot-2.png?sign=def',
    original_name: 'a<b>"c".png',
    size_bytes: 2048,
    remark: '',
    uploaded_by: 7,
    created_at: '2026-10-23T10:01:00+08:00',
  },
]

/**
 * layui 的**可控替身**（挂载整页用）。`bootstrapIsland` 在 jsdom 里加载不了真的 layui，
 * 所以把它换掉，`table.render` / `table.reload` / `layer.confirm` 的**入参**才能被断言
 * （结构与 `order-orderlist.spec.ts:37-84` 同形）。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  /**
   * formSelects 的**可控替身**（终审 Critical 1：此前根本没有它，所以「隐藏 `<select>` 与
   * widget 谁说了算」这条路径完全没被覆盖）。
   *
   * 三个方法都按 vendored 源的真实形状：
   *  - `value(id)` 返回当前选中值（`formselects-v4.js:1508`）；
   *  - `data(id, 'local', cfg)` **先 `this.value(id, [])` 清空选中**（`:1696`）；
   *  - `on(id, cb)` 只登记回调——`cb` 的第二个参数是**改动前**的选中值（`:1164/1177/1182`），
   *    回归用例靠它复现旧同步监听器的注入形状（若监听器复活就调用它）。
   */
  const formSelectsState: { current: unknown[] } = { current: [] }
  const formSelectsHandlers = new Map<string, (...args: unknown[]) => unknown>()
  /**
   * 挂载顺序的**运行期**判据（`AGENTS.md` §5-G 陷阱③b；先例 = `thirdparty-jidanpu-sync.spec.ts`
   * 的 `seenByInit`）：记 `initIslandElements` 被调用那一刻，页面标记里的 `#tableList` 在不在
   * DOM 里。标记先塞 ⇒ `[true]`；顺序反了（先 init 再塞标记）⇒ `[false]`。
   * ⚠️ 那个先例 spec 已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）——本条判据照旧生效。
   */
  const seenByInit: boolean[] = []
  return {
    msg,
    tableHandlers,
    formHandlers,
    formSelectsState,
    formSelectsHandlers,
    seenByInit,
    formSelects: {
      value: vi.fn(() => formSelectsState.current),
      data: vi.fn<(id: string, type: string, opts: unknown) => void>(() => {
        formSelectsState.current = []
      }),
      on: (id: string, cb: (...args: unknown[]) => unknown) => formSelectsHandlers.set(id, cb),
    },
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: {
      render: vi.fn(),
      reload: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: {
      msg,
      open: vi.fn(),
      close: vi.fn(),
      confirm: vi.fn(),
      prompt: vi.fn(),
      tips: vi.fn(),
      photos: vi.fn(),
      // ⭐ 抢单成功的「未能撤回」警告走 `layer.alert`（停留更久、要人工确认）。
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
    // ⚠️ 换掉它只为记**运行期顺序**（`AGENTS.md` §5-G 陷阱③b）：真实现的 `element.init` 在
    //    本文件里没有任何断言，替身不影响其余用例。
    initIslandElements: vi.fn(() => {
      LAYUI.seenByInit.push(document.querySelector('#tableList') !== null)
    }),
  }
})

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return {
    ...actual,
    fetchGames: vi.fn(async () => [
      { id: 3, name: '原神', sort_order: 1, is_active: true, created_at: '' },
    ]),
    fetchServers: vi.fn(async () => [
      { id: 7, name: '天空岛', game: 3, sort_order: 1, is_active: true, created_at: '' },
    ]),
  }
})

/**
 * 权限门（`AGENTS.md` §5-D / D-0154 / E-0098）要读 `/api/auth/me` ⇒ 必须打桩：
 * 一是判据要可控（持节点 / 不持节点），二是**零真网络**（`request()` 走 axios 实例，
 * 全局 `fetch` 的桩拦不到它）。
 */
vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, fetchMe: vi.fn() }
})

/**
 * 「参考图」要打 `GET /api/business/orders/<单号>/screenshots`（`api/screenshots.ts`）⇒ 打桩。
 * ⚠️ 这是**真网络出口**（`request()` 走 axios 实例，全局 `fetch` 的桩拦不到它）——不打桩时
 * 「没图 / 403 / 网络失败」三条用例会真的去连后端，失败形态还会随环境漂移。
 */
vi.mock('@/api/screenshots', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/screenshots')>()
  return { ...actual, fetchScreenshots: vi.fn() }
})

import { fetchMe, type User } from '@/api/auth'

/** 持 `grab_hall.take_order` 的打手（非管理员 ⇒ 只能靠 `menu_permissions`）。 */
const TAKER_ME: User = {
  id: 7,
  username: 'taker-a',
  display_name: '打手甲',
  identity_plane: 'tenant',
  is_tenant_admin: false,
  is_account_admin: false,
  roles: ['代练员'],
  permissions: [],
  menu_permissions: ['grab_hall', 'grab_hall.take_order', 'order.view_screenshot'],
  game_ids: [3],
}

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** 挂载整页（证据件走全局 fetch，字典走 `@/api/config` 的替身）。 */
async function mountHall(): Promise<{ root: HTMLElement; cleanup: () => void }> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountHallPage(root, { onNavigate: vi.fn() })
  return { root, cleanup }
}

beforeEach(() => {
  LAYUI.table.render.mockClear()
  LAYUI.table.reload.mockClear()
  LAYUI.msg.mockClear()
  LAYUI.layer.open.mockClear()
  LAYUI.layer.close.mockClear()
  LAYUI.layer.confirm.mockClear()
  LAYUI.layer.photos.mockClear()
  LAYUI.layer.alert.mockClear()
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  // widget 的选中值在每个用例里从「未选」开始；`data()` 的清空语义由替身自己实现。
  LAYUI.formSelectsState.current = []
  LAYUI.formSelectsHandlers.clear()
  LAYUI.seenByInit.length = 0
  LAYUI.formSelects.value.mockClear()
  LAYUI.formSelects.data.mockClear()
  vi.mocked(claimOrder).mockReset()
  // ⚠️ 默认给一个**已兑现**的返回值（执行期修订）：真 `claimOrder` 恒返回 `Promise<Order>`，
  //    而 `mockReset()` 后是 `undefined` ⇒ 只调 `yes(index)` 而不断言结果的用例会在
  //    `claimOrder(...).then` 上抛 `TypeError`（那测的是替身的形状，不是页面的行为）。
  //    与 `src/api/__tests__/orders.spec.ts` 的 `mockReset(); mockResolvedValue({})` 同形。
  vi.mocked(claimOrder).mockResolvedValue({} as never)
  // ⭐ 默认「后端 msg 为空、抢单成功」——既有用例只配 `claimOrder` 即可驱动全流程。
  vi.mocked(claimOrderWithMessage).mockReset()
  vi.mocked(claimOrderWithMessage).mockImplementation(
    (async (orderNo: string) => ({
      order: await vi.mocked(claimOrder)(orderNo),
      message: '',
    })) as never,
  )
  // 权限门：默认「持节点」⇒ 原生行的两颗按钮与「参考图」照常画（**零真网络**：`/api/auth/me` 不打）。
  // 两个节点都给：`grab_hall.take_order`（接单）+ `order.view_screenshot`（看参考图），
  // 与 2026-10-20 之后新建租户的「代练员」seed 一致（`apps/rbac/seed.py`）。
  vi.mocked(fetchMe).mockReset()
  vi.mocked(fetchMe).mockResolvedValue(TAKER_ME)
  // 「参考图」的取数出口：默认「这单没图」（空列表）——各用例按需改。
  vi.mocked(fetchScreenshots).mockReset()
  vi.mocked(fetchScreenshots).mockResolvedValue({ items: [], total: 0 })
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

/**
 * 造一棵与证据件同形的筛选树（只保留本页真正读取的 4 个控件）。
 *
 * ⚠️ 「没选中」的占位 option 必须是 `value=""`（证据件里 `#gameArea` 的占位就是
 * `<option value="">请选择或输入</option>`）。**不能写 `value="0"`**——`toId('0')` 是 `0`
 * 而不是「空」（E-0021：0 是有意义的边界值，判据必须是「空」不是「假值」；后端
 * `?game=0` 也会被 `min_value=1` 拒掉）。执行期实测：写成 `value="0"` 时
 * 「全空」用例拿到 `{game:0, server:0}`，与本用例「交给 toHallParams 丢干净」的判据相反。
 */
function filterHost(): HTMLElement {
  const root = document.createElement('div')
  root.innerHTML = `
    <select id="gameName" name="gameName"><option value=""></option><option value="3">原神</option></select>
    <select id="gameArea" name="gameArea"><option value=""></option><option value="7">天空岛</option></select>
    <input type="text" name="key1" id="key1" value="" />
    <input type="text" name="project" id="project" value="" />`
  return root
}

describe('大厅筛选 → 我们的检索参数（4 个控件 / 6 个后端参数）', () => {
  it('选择游戏/服务区·平台/搜索/项目 → game/server/keyword/project_text', () => {
    const root = filterHost()
    root.querySelector<HTMLSelectElement>('#gameName')!.value = '3'
    root.querySelector<HTMLSelectElement>('#gameArea')!.value = '7'
    root.querySelector<HTMLInputElement>('#key1')!.value = ' 角色名 '
    root.querySelector<HTMLInputElement>('#project')!.value = ' 深渊满星 '
    expect(toHallSearch(root)).toEqual({
      game: 3,
      server: 7,
      keyword: '角色名',
      project_text: '深渊满星',
    })
  })

  it('全空 → 四个值都是空（game/server=null，文本=空串），交给 toHallParams 丢干净', () => {
    const root = filterHost()
    expect(toHallSearch(root)).toEqual({ game: null, server: null, keyword: '', project_text: '' })
    // 「空值不发」这条规则只在 api 层实现一次（Task 1 的 toHallParams）
    expect(toHallParams(toHallSearch(root))).toEqual({})
  })

  it('区服读取是**注入的接缝**：xm-select 的选中值同步到隐藏 <select>，读不到时回落到它', () => {
    const root = filterHost()
    expect(toHallSearch(root, () => 42).server).toBe(42)
    expect(toHallSearch(root, () => null).server).toBe(null)
  })

  it('价格区间在后端支持，但本页没有控件 → 永远不出现在参数里（差异表 #10）', () => {
    expect(Object.keys(toHallParams(toHallSearch(filterHost())))).toEqual([])
  })
})

describe('#total 句子：只替换真实的「订单数」，总金额保持证据件的 0', () => {
  it('在**证据件原文**上替换（模板改了这条会红）', () => {
    const text = /<div[^>]*id="total"[^>]*>\s*([\s\S]*?)\s*<\/div>/.exec(EVIDENCE)![1]!
    const out = renderHallTotal(text, 7)
    expect(out).toContain('当前共有7笔订单等待接单，总金额0元')
    // 句子前缀（含租户名占位「—」）逐字不变
    expect(out.startsWith('本系统为—内部订单管理系统')).toBe(true)
  })

  it('认不出句子时原样返回（不抛、不编）', () => {
    expect(renderHallTotal('完全不是那句话', 7)).toBe('完全不是那句话')
  })
})

describe('buildHallCols：声明骨架逐项来自 COLS，只挂数据渲染', () => {
  // `canClaim: true` = 过权限门（本组测的是**列渲染**，权限门本身由 remote spec 与下面
  // 「按钮 class 与 data-id」那条一起钉）。原生行 ⇒ 两列仍是「接手此单」。
  // `canViewShots: true` = 过「参考图」的节点门（原生行 ⇒ 画那颗 `mangeImg`）。
  const cols = buildHallCols({
    gameName: (id) => `G${id}`,
    serverName: (id) => `S${id}`,
    canClaim: true,
    canViewShots: true,
  })
  const cell = (i: number): ((row: HallListItem) => string) =>
    cols[i]!.templet as (row: HallListItem) => string

  it('15 列，且 field/title/width/minWidth/hide/align/fixed/sort 与 COLS **逐项相同**', () => {
    expect(cols).toHaveLength(COLS.length)
    cols.forEach((col, i) => {
      const base = COLS[i]!
      expect(col.field).toBe(base.field)
      expect(col.title).toBe(base.title)
      expect(col.width).toBe(base.width)
      expect(col.minWidth).toBe(base.minWidth)
      expect(col.hide).toBe(base.hide)
      expect(col.align).toBe(base.align)
      expect(col.sort).toBe(base.sort)
    })
  })

  it('订单号列：加急徽标 + `.orderdetails` 蓝链 + 转义过的 title（差异表 #17）', () => {
    const html = cell(1)({ ...ROW, is_urgent: true, project_text: 'a"b' })
    expect(html).toContain('background-color:#FFBBFF')
    expect(html).toContain('class="orderdetails"')
    expect(html).toContain('title="a&quot;b，只有接下此单才能看详情"')
    expect(html).toContain('>20260920213215233029</a>')
  })

  it('项目列渲染 `project_text`，且对 `<`/`>` 转义（差异表 #17）', () => {
    expect(cell(5)({ ...ROW, project_text: '深渊满星' })).toBe('深渊满星')
    expect(cell(5)({ ...ROW, project_text: 'a<b>c' })).toBe('a&lt;b&gt;c')
  })

  it('接手价列渲染 `hall_price`（**生效接单价**）；为 null 时输出空（不编 0）', () => {
    expect(cell(9)(ROW)).toContain('你接手此单的金额：100.00元')
    expect(cell(9)({ ...ROW, hall_price: null })).toContain('你接手此单的金额：元')
  })

  it('角色信息列 = `[游戏名]区服名`；无数据的 6 列恒空（差异表 #1–#6）', () => {
    expect(cell(6)(ROW)).toBe('<span title="">[G2]S7</span>')
    for (const i of [2, 3, 4, 8, 10, 12]) expect(cell(i)(ROW)).toBe('')
  })

  it('订单状态列：恒绿徽标 + `orderStatusLabel`（对标这一列没有 switch）', () => {
    expect(cell(7)(ROW)).toBe(
      '<a href="javascript:void(0)" class="lableb setdlsta" data-id="20260920213215233029" title="等待接单" style="background-color:#219227;">待抢单</a>',
    )
  })

  it('参考图列渲染对标的 `mangeImg` 按钮（data-id = 我方单号；差异表 #7 已实装）', () => {
    const html = cell(11)(ROW)
    expect(html).toContain('class="layui-btn layui-btn-xs layui-btn-normal mangeImg"')
    expect(html).toContain('title="查看参考图"')
    // 点击时用它去打 `/api/business/orders/<单号>/screenshots`
    expect(html).toContain('data-id="20260920213215233029"')
    // 镜像行没有我方单号（本系统没有它的截图）⇒ ⛔ 不画一颗只会换来「记录不存在」的按钮
    expect(
      cell(11)({ ...ROW, origin: 'remote', order_no: '', remote_order_id: 'REMOTE-1' }),
    ).toBe('')
  })

  it('参考图列是**权限门**：没 `order.view_screenshot` ⇒ 整格为空（fail-closed，D-0154/E-0098）', () => {
    // 字面值 = 后端 `screenshot_views.SCREENSHOT_VIEW_NODE`（`order.view_screenshot`）
    expect(SCREENSHOT_VIEW_NODE).toBe('order.view_screenshot')
    const noShots = buildHallCols({
      gameName: () => 'G',
      serverName: () => 'S',
      canClaim: true,
      canViewShots: false,
    })
    const templet = noShots[11]!.templet as (row: HallListItem) => string
    expect(templet(ROW)).toBe('')
  })
})

/** 一次挂载，返回 `table.render` 收到的配置（唯一的一发）。
 *
 * 放在模块作用域（不在 `describe` 里）：抢单那组也要用它把「当前页」推到第 2 页。 */
async function mountAndConfig(): Promise<{
  root: HTMLElement
  cleanup: () => void
  cfg: Record<string, unknown>
}> {
  const { root, cleanup } = await mountHall()
  expect(LAYUI.table.render).toHaveBeenCalledTimes(1)
  return { root, cleanup, cfg: LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown> }
}

/** 最近一次 `table.reload` 的 `where`（`toHallParams` 之后的**实际**请求参数）。 */
function lastReloadWhere(): Record<string, unknown> {
  const calls = LAYUI.table.reload.mock.calls
  const call = calls[calls.length - 1]
  expect(call, 'table.reload 没有被调用').toBeTruthy()
  return (call![1] as { where: Record<string, unknown> }).where
}

/** 最近一次 `table.reload` 的第 2 参（整个 payload）。 */
function lastReloadPayload(): { where: Record<string, unknown>; page: Record<string, unknown> } {
  const calls = LAYUI.table.reload.mock.calls
  const call = calls[calls.length - 1]
  expect(call, 'table.reload 没有被调用').toBeTruthy()
  return call![1] as { where: Record<string, unknown>; page: Record<string, unknown> }
}

describe('挂载：真表 + 真筛选 + 服务端分页', () => {
  /** 点页内某颗元素（bubbles 必须为 true：本页用 root 上的委托）。 */
  function click(root: HTMLElement, selector: string): void {
    const el = root.querySelector(selector)
    expect(el, `页内找不到 ${selector}`).not.toBeNull()
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  it('挂载顺序③b：`initIslandElements` 跑的时候页面标记**已经在 DOM 里**（AGENTS.md §5-G / E-0061 / E-0067）', async () => {
    // 先例 = `thirdparty-jidanpu-sync.spec.ts` 的 `seenByInit`：判据是**运行期相对顺序**，
    // ⛔ 不是「这个函数被调用过」——被调用过但早于 `root.innerHTML = …` 时页面照样是错的
    // （`initIslandElements` 要按标记里的 `lay-*` 属性初始化，跑早了等于没初始化）。
    // ⚠️ 上面那个先例 spec 已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）。
    const { cleanup } = await mountHall()
    expect(LAYUI.seenByInit).toEqual([true])
    cleanup()
  })

  it('table.render 的落地参数逐项等于对标（url / pageName / limits / toolbar / defaultToolbar / 15 列）', async () => {
    const { cleanup, cfg } = await mountAndConfig()
    expect(cfg.elem).toBe('#tableList')
    expect(cfg.id).toBe('tableList')
    expect(cfg.url).toBe('/api/business/hall/')
    expect(cfg.method).toBe('get')
    // 首屏也要带 `game`（对标首屏 URL 就带 gameName）——不带的后果是别的游戏的区服渲染成裸主键
    expect(cfg.where).toEqual({ game: 3 })
    expect(cfg.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    expect(cfg.page).toEqual({ limit: 20, limits: [10, 20] })
    // 传给 layui 的 `page` / `defaultToolbar` 必须是**拷贝**：layui 会就地改 `page`
    // （写 `curr`、删 `elem`/`jump`），直接传导出的常量会让 `order-replica.spec.ts` 的断言漂移。
    expect(cfg.page).not.toBe(TABLE.page)
    expect(cfg.defaultToolbar).not.toBe(TABLE.defaultToolbar)
    expect(TABLE.page).toEqual({ limit: 20, limits: [10, 20] })
    expect(cfg.toolbar).toBe('#toolbarDemo')
    expect(cfg.defaultToolbar).toEqual(['filter'])
    expect((cfg.cols as unknown[][])[0]).toHaveLength(15)
    // 载荷层面的同一事实：首屏按选中游戏筛 ⇒ 这一页的行都属于 `game 3`，列渲染器对它们
    // 拿得到真名（`原神` / `天空岛`），不会落到 `String(id)` 的裸主键兜底（`[原神]7`）。
    const infoCol = (cfg.cols as { templet: (row: HallListItem) => string }[][])[0]![6]!
    expect(infoCol.templet({ ...ROW, game: 3, server: 7 })).toBe('<span title="">[原神]天空岛</span>')
    cleanup()
  })

  it('parseData 解统一信封：count 取 total、data 取 items', async () => {
    const { cleanup, cfg } = await mountAndConfig()
    const parse = cfg.parseData as (r: unknown) => { code: number; count: number; data: unknown[] }
    expect(parse({ code: 0, msg: '', data: { items: [ROW], total: 41 } })).toEqual({
      code: 0,
      msg: '',
      count: 41,
      data: [ROW],
    })
    // 后端漏 items/total 时也不抛（回落成空页）
    expect(parse({ code: 0, msg: '', data: undefined })).toMatchObject({ count: 0, data: [] })
    cleanup()
  })

  it('游戏下拉 = 我们的游戏（value = 主键）；区服下拉有选项但**不预选**（对标也是未选中）', async () => {
    const { root, cleanup } = await mountAndConfig()
    const game = root.querySelector<HTMLSelectElement>('select#gameName')!
    expect(game.value).toBe('3')
    expect(game.options[0]!.textContent).toBe('原神')
    const area = root.querySelector<HTMLSelectElement>('select#gameArea')!
    // ⚠️ **不能断言 `area.value === '7'`**（执行期修订）：那正是「界面显示未选、请求却按第一个
    // 区服筛」的静默收窄。证据件里 `#gameArea` 第一项是 `<option value="">请选择或输入</option>`。
    expect(area.value).toBe('')
    expect(area.options[0]!.value).toBe('')
    expect(area.querySelector('option[value="7"]')?.textContent).toBe('天空岛')
    cleanup()
  })

  it('点「刷新列表」→ table.reload(tableList, {where, page:{curr:1}}) ；筛选值进 where、空值不发', async () => {
    const { root, cleanup } = await mountAndConfig()
    root.querySelector<HTMLInputElement>('#key1')!.value = '角色名'
    click(root, 'a.refresh')
    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList', {
      // game 来自下拉（挂载时已选中第一条 = 我们的 3）；**区服未预选 ⇒ 不进 where**；
      // project 为空 → 被 toHallParams 丢掉
      where: { game: 3, keyword: '角色名' },
      // ⚠️ **没有 `limit`**：layui 自己那份实例 `limit`（用户选的「条/页」）
      // 由它的分页条 `jump` 维护，我们塞常量会盖回去。`page` 只有 `curr` 一个键。
      page: { curr: 1 },
    })
    expect(Object.keys(lastReloadPayload().page)).toEqual(['curr'])
    cleanup()
  })

  it('搜索按钮（form.on("submit(seachBtn)")）→ reload(1)，且不塞 limit', async () => {
    const { cleanup } = await mountAndConfig()
    const onSearch = LAYUI.formHandlers.get('submit(seachBtn)')
    expect(onSearch).toBeTypeOf('function')
    onSearch!()
    expect(LAYUI.table.reload).toHaveBeenCalledWith(
      'tableList',
      expect.objectContaining({ page: { curr: 1 } }),
    )
    cleanup()
  })

  it('回车 = 搜索（只在 INPUT 上生效；对标 `$(document).keyup(getKey)`）', async () => {
    const { root, cleanup } = await mountAndConfig()
    root
      .querySelector<HTMLInputElement>('#key1')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(LAYUI.table.reload).toHaveBeenCalledWith(
      'tableList',
      expect.objectContaining({ page: { curr: 1 } }),
    )
    // 在非 INPUT 上回车不触发（否则页内任意点击 + 回车都会重载）
    LAYUI.table.reload.mockClear()
    root
      .querySelector<HTMLSelectElement>('select#gameName')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    cleanup()
  })

  it('done 记下真实页码（真人翻页）；「刷新列表」回第 1 页但**不带 limit**（layui 的「条/页」不被盖回）', async () => {
    const { root, cleanup, cfg } = await mountAndConfig()
    // layui 2.13.9 的翻页钩子是 `done(res, curr, count, origin)`（`page(<id>)` 事件不存在）。
    ;(cfg.done as (r: unknown, curr: number, count: number) => void)(undefined, 2, 40)
    click(root, 'a.refresh')
    expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList', {
      where: { game: 3 },
      page: { curr: 1 },
    })
    // 显式判据：`page` 里**没有** `limit` 键（不是「limit 恰好是 20」）
    expect(Object.keys(lastReloadPayload().page)).toEqual(['curr'])
    cleanup()
  })

  it('切游戏（form.on("select(selectGame)")）→ 按新游戏重取区服 → reload(1)', async () => {
    const { root, cleanup } = await mountAndConfig()
    vi.mocked(fetchServers).mockClear()
    // `#gameName` 的选项由页面按 `fetchGames()` 重灌（这里只有 id=3 一个）——要让 `<select>`
    // 真的报告新游戏，得先有对应的 `<option>`（否则 `select.value = '9'` 会被折叠成 ''）。
    const game = root.querySelector<HTMLSelectElement>('select#gameName')!
    const extra = document.createElement('option')
    extra.value = '9'
    extra.textContent = '星穹铁道'
    game.appendChild(extra)
    game.value = '9'
    expect(game.value).toBe('9')
    const onGame = LAYUI.formHandlers.get('select(selectGame)')
    expect(onGame).toBeTypeOf('function')
    onGame!()
    await vi.waitFor(() => {
      expect(vi.mocked(fetchServers)).toHaveBeenCalledWith(9)
    })
    await vi.waitFor(() => {
      expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList', {
        where: { game: 9 },
        page: { curr: 1 },
      })
    })
    cleanup()
  })

  it('区服回落：隐藏 <select> 为空时取 widget 的当前值（对象形与裸值形都认）', async () => {
    const { root, cleanup } = await mountAndConfig()
    expect(root.querySelector<HTMLSelectElement>('select#gameArea')!.value).toBe('')
    LAYUI.formSelectsState.current = [{ value: 7 }]
    click(root, 'a.refresh')
    expect(lastReloadWhere()).toEqual({ game: 3, server: 7 })
    // 裸值形（formSelects 的 `value(id,'val')`）也认
    LAYUI.table.reload.mockClear()
    LAYUI.formSelectsState.current = [7]
    click(root, 'a.refresh')
    expect(lastReloadWhere()).toEqual({ game: 3, server: 7 })
    cleanup()
  })

  it('区服不残留：formSelects.data() 清空选中（vendored :1696）⇒ server 不发', async () => {
    const { root, cleanup } = await mountAndConfig()
    LAYUI.formSelectsState.current = [{ value: 7 }]
    LAYUI.formSelects.data('gameArea', 'local', { arr: [{ name: '天空岛', value: 7 }] })
    expect(LAYUI.formSelectsState.current).toEqual([])
    click(root, 'a.refresh')
    expect(lastReloadWhere()).toEqual({ game: 3 })
    cleanup()
  })

  it('回归：旧的「同步回隐藏 <select>」监听器不得复活（widget=9、select 被写成 7 ⇒ server=9）', async () => {
    const { root, cleanup } = await mountAndConfig()
    // 旧实现的注入形状：formSelects 的 `on(id, vals)` 给的是**改动前**的值（vendored `:1164/1177/1182`）。
    // 若监听器复活，下面这行会把 7 写进隐藏 <select>；修好后它是 no-op、隐藏 <select> 保持空。
    LAYUI.formSelectsHandlers.get('gameArea')?.('gameArea', [{ value: 7 }])
    LAYUI.formSelectsState.current = [{ value: 9 }]
    click(root, 'a.refresh')
    expect(lastReloadWhere()).toEqual({ game: 3, server: 9 })
    // 结构性判据：页面压根**不登记**这个回调（旧同步的注册点）。
    expect(LAYUI.formSelectsHandlers.has('gameArea')).toBe(false)
    cleanup()
  })

  it('点「参考图」有图：按**我方单号**拉列表 → `layer.photos`（标题 / 图片数 / src / 说明文字）', async () => {
    const { root, cleanup } = await mountAndConfig()
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: SHOTS, total: SHOTS.length })
    root.appendChild(shotButtonNode(ROW.order_no))
    click(root, '.mangeImg')
    await vi.waitFor(() => expect(LAYUI.layer.photos).toHaveBeenCalledTimes(1))
    // 取数走的是「这一行」的单号（原生行的 data-id = 我方单号），不是列表里别的行
    expect(vi.mocked(fetchScreenshots).mock.calls[0]).toEqual([ROW.order_no])
    const opts = LAYUI.layer.photos.mock.calls[0]![0] as {
      photos: { title: string; data: { alt: string; src: string }[] }
    }
    expect(opts.photos.title).toBe('查看参考图')
    // 图片数 = 后端回来的条数（不是固定 1 张、也不是 0）
    expect(opts.photos.data).toHaveLength(SHOTS.length)
    expect(opts.photos.data[0]).toEqual({
      // 说明 = 原文件名（有备注时括注）
      alt: '进度1.png（第一关）',
      // src 原样取后端签发的 URL（⛔ 前端不自己拼）
      src: 'https://cos.example/shot-1.png?sign=abc',
    })
    // 第二个字段来自上传人写的自由文本（`<` / 引号）⇒ 过 `escapeHtml`（vendored layui 拼进
    // `<img alt="…">` 的属性，不转义就是存储型 XSS；先例 emp-settlementemp 的那条 Important）
    expect(opts.photos.data[1]!.alt).toBe('a&lt;b&gt;&quot;c&quot;.png')
    // 有图时**不**弹「没图」那句
    expect(LAYUI.msg).not.toHaveBeenCalled()
    cleanup()
  })

  it('点「参考图」没图：提示「这单还没有上传过截图」，且**不开**看图', async () => {
    const { root, cleanup } = await mountAndConfig()
    vi.mocked(fetchScreenshots).mockResolvedValue({ items: [], total: 0 })
    root.appendChild(shotButtonNode(ROW.order_no))
    click(root, '.mangeImg')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledTimes(1))
    // 钉**字面值**：这是给客服看的一句人话，语义是「没图」，不是「坏了」
    expect(LAYUI.msg.mock.calls[0]![0]).toBe('这单还没有上传过截图')
    expect(LAYUI.layer.photos).not.toHaveBeenCalled()
    cleanup()
  })

  it('点「参考图」403：原样弹后端 msg（「你没有订单截图相关的权限」），且**不开**看图', async () => {
    const { root, cleanup } = await mountAndConfig()
    vi.mocked(fetchScreenshots).mockRejectedValue(
      new HttpError(403, '你没有订单截图相关的权限'),
    )
    root.appendChild(shotButtonNode(ROW.order_no))
    click(root, '.mangeImg')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledTimes(1))
    // 后端 403 的 msg 由 `api/http.ts` 的 `HttpError` 带回来 ⇒ 本页原样显示（⛔ 不静默、不换文案）
    expect(LAYUI.msg.mock.calls[0]![0]).toBe('你没有订单截图相关的权限')
    expect(LAYUI.layer.photos).not.toHaveBeenCalled()
    cleanup()
  })

  it('点「参考图」网络失败：弹 NetworkError 的人话，且**不开**看图（⛔ 不静默）', async () => {
    const { root, cleanup } = await mountAndConfig()
    vi.mocked(fetchScreenshots).mockRejectedValue(new NetworkError('网络异常，请检查连接'))
    root.appendChild(shotButtonNode(ROW.order_no))
    click(root, '.mangeImg')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledTimes(1))
    expect(LAYUI.msg.mock.calls[0]![0]).toBe('网络异常，请检查连接')
    expect(LAYUI.layer.photos).not.toHaveBeenCalled()
    cleanup()
  })

  it('table.done 用真实 total 改写 #total 的订单数', async () => {
    const { root, cleanup, cfg } = await mountAndConfig()
    ;(cfg.done as (r: { count?: number }) => void)({ count: 12 })
    expect(root.querySelector('#total')!.textContent).toContain('当前共有12笔订单等待接单，总金额0元')
    cleanup()
  })
})

/** 造一颗真 templet 渲染出来的抢单按钮（页内委托读 `data-id` / `data-origin`）。 */
function claimButtonNode(orderNo: string): HTMLElement {
  const box = document.createElement('div')
  box.innerHTML =
    `<a class="${CLAIM_BUTTON_CLASS}" data-id="${orderNo}" ` +
    `data-origin="native" data-source="">接手此单</a>`
  return box.firstElementChild as HTMLElement
}

/**
 * 造一颗「参考图」按钮（表体由 layui 渲染 ⇒ jsdom 里没有真表，手工放同 class 的节点）。
 * `data-id` 就是原生行的我方单号（与 templet 渲染出来的形状一致）。
 */
function shotButtonNode(orderNo: string): HTMLElement {
  const box = document.createElement('div')
  box.innerHTML =
    `<button class="layui-btn layui-btn-xs layui-btn-normal ${SHOT_BUTTON_CLASS}" ` +
    `data-id="${orderNo}" data-origin="native" title="查看参考图">参考图</button>`
  return box.firstElementChild as HTMLElement
}

/** 走完整链路：挂载 → 点击抢单按钮 → 取出 confirm 的回调 → 调用它。 */
async function clickClaim(): Promise<{ root: HTMLElement; cleanup: () => void }> {
  const { root, cleanup } = await mountHall()
  root.appendChild(claimButtonNode(ROW.order_no))
  root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
  const [text, yes] = LAYUI.layer.confirm.mock.calls[0] as [string, (index: number) => void]
  expect(text).toBe(CLAIM_CONFIRM_TEXT)
  expect(yes).toBeTypeOf('function')
  yes(5)
  return { root, cleanup }
}

describe('抢单：对标确认语 → claimOrder → 就地刷新；失败显示后端 msg', () => {
  it('按钮 class / 文案 / 确认语 / 权限节点钉的是**字面值**（E-0024：期望值写字面值，不拿常量断言常量）', () => {
    // 其余用例用 `CLAIM_BUTTON_CLASS` 造按钮、并与它比较——那是「测试与实现同一份、避免漂移」的
    // 刻意选择，代价是常量被改坏时那些用例**不会红**。本条把四个常量钉回**外部契约原文**
    // （三个对标值 + 一个后端节点 code），补上那个缺口。
    expect(CLAIM_BUTTON_CLASS).toBe('layui-btn layui-btn-danger layui-btn-xs recThisOrder')
    expect(CLAIM_BUTTON_TEXT).toBe('接手此单')
    expect(CLAIM_CONFIRM_TEXT).toBe(
      '确定接下此单吗？【提醒】本系统不属于交易平台，不提供担保，订单结算需要自行和本商户联系',
    )
    // ⭐ 权限节点 = 后端 `ORDER_PERMISSIONS["claim"]` / `["remote_claim"]` 的**字面值**
    // （`apps/business/api/order_views.py`）。⛔ 不是栏目码 `grab_hall`（那只管进页）。
    expect(CLAIM_NODE).toBe('grab_hall.take_order')
  })

  it('actioncol / actioncol2 都渲染对标的「接手此单」按钮（class / data-id / 两源标识逐字）', () => {
    const cols = buildHallCols({
      gameName: () => 'G',
      serverName: () => 'S',
      canClaim: true,
      canViewShots: true,
    })
    for (const i of [0, 14]) {
      const html = (cols[i]!.templet as (d: HallListItem) => string)(ROW)
      // 原生行：`data-origin="native"`、`data-source=""`（`data-source` 是行来源标记；
      // 接单分派**不**读它——`remote` 那条路已拆除）。
      expect(html).toBe(
        `<a class="${CLAIM_BUTTON_CLASS}" data-id="20260920213215233029" ` +
          `data-origin="native" data-source="">接手此单</a>`,
      )
    }
  })

  it('点它 → layer.confirm 用对标的确认语，且第二参是回调（与对标 `layer.confirm(text, fn)` 同形）', async () => {
    const { cleanup } = await clickClaim()
    expect(LAYUI.layer.confirm.mock.calls[0]![0]).toBe(CLAIM_CONFIRM_TEXT)
    cleanup()
  })

  it('确认回调：关弹层（用它的层索引）+ claimOrder(单号)', async () => {
    const { cleanup } = await clickClaim()
    expect(LAYUI.layer.close).toHaveBeenCalledWith(5)
    expect(vi.mocked(claimOrder)).toHaveBeenCalledWith(ROW.order_no)
    cleanup()
  })

  it('抢单成功 → 就地刷新当前页（page.curr = 当前页，不是回第 1 页）+ 成功提示', async () => {
    const { root, cleanup, cfg } = await mountAndConfig()
    // 1) 先把「当前页」推到第 2 页：真人翻页时 layui 会以 `done(res, curr, count, origin)` 交回
    //    真实页码（`page(<id>)` 事件在 2.13.9 里不存在）；jsdom 里手动调用 render 配置的 `done`。
    ;(cfg.done as (r: unknown, curr: number, count: number) => void)(undefined, 2, 20)
    // 2) 点抢单并确认（断言「刷新的是第 2 页」——这条如果写成先 yes() 再改页码，测的就是第 1 页）
    vi.mocked(claimOrder).mockResolvedValue({} as never)
    root.appendChild(claimButtonNode(ROW.order_no))
    root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const yes = LAYUI.layer.confirm.mock.calls[0]![1] as (index: number) => void
    yes(5)
    await vi.waitFor(() => {
      expect(LAYUI.table.reload).toHaveBeenCalledWith('tableList', {
        where: { game: 3 },
        page: { curr: 2 },
      })
    })
    // **只抢一次**（不重复发请求），且 `page` 里没有 `limit`（layui 的「条/页」不被盖回）
    expect(vi.mocked(claimOrder)).toHaveBeenCalledTimes(1)
    expect(Object.keys(lastReloadPayload().page)).toEqual(['curr'])
    // 成功提示在刷新**之后**：`layer.msg` 是后来的覆盖先前的，顺序反了用户只看到刷新的副作用提示。
    expect(LAYUI.msg).toHaveBeenCalledWith('抢单成功', { time: 1500 })
    expect(LAYUI.table.reload.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.msg.mock.invocationCallOrder[0]!,
    )
    cleanup()
  })

  it('抢单成功但后端 msg 带「未能撤回」警告 ⇒ **原文**用 `layer.alert` + 刷新（⛔ 不吞）', async () => {
    const WARNING =
      '代练通订单未能撤回（原话：可操作资金不足，不能发布.），' +
      '请先到「第三方平台订单管理」撤回代练通平台订单'
    vi.mocked(claimOrderWithMessage).mockResolvedValue({
      order: {} as never,
      message: WARNING,
    } as never)
    const { root, cleanup } = await mountAndConfig()
    root.appendChild(claimButtonNode(ROW.order_no))
    root.querySelector('.recThisOrder')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const yes = LAYUI.layer.confirm.mock.calls[0]![1] as (index: number) => void
    yes(5)
    await vi.waitFor(() => expect(LAYUI.layer.alert).toHaveBeenCalled())
    expect(LAYUI.layer.alert).toHaveBeenCalledWith(
      WARNING,
      expect.objectContaining({ title: '注意：代练通订单未能撤回' }),
    )
    expect(LAYUI.msg).not.toHaveBeenCalledWith('抢单成功', { time: 1500 })
    expect(LAYUI.table.reload).toHaveBeenCalled()
    cleanup()
  })

  it('HTTP 403：页面显示**后端 msg 原文**，不换成自己的文案（统一分支）', async () => {
    vi.mocked(claimOrder).mockRejectedValue(new HttpError(403, '只有代练员可以抢单'))
    const { cleanup } = await clickClaim()
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('只有代练员可以抢单', { time: 1500 })
    })
    cleanup()
  })

  it('业务码 1012：显示后端 msg 原文', async () => {
    vi.mocked(claimOrder).mockRejectedValue(new BizError(1012, '该订单已被别人接走'))
    const { cleanup } = await clickClaim()
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('该订单已被别人接走', { time: 1500 })
    })
    cleanup()
  })

  it('业务码 1013：显示后端 msg 原文（四支限制共用码，靠 msg 区分）', async () => {
    vi.mocked(claimOrder).mockRejectedValue(new BizError(1013, '你手上未完成的订单已达 10 单'))
    const { cleanup } = await clickClaim()
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('你手上未完成的订单已达 10 单', { time: 1500 })
    })
    cleanup()
  })
})
