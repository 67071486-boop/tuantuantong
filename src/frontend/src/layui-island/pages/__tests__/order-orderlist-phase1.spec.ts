// @vitest-environment jsdom
import { renderOrderRows } from './order-list-table-fixture'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  BUSS_TYPE_TO_ORDER_TYPE,
  buildOrderCols,
  CONTEXT_MENU_ITEMS,
  CONTEXT_MENU_STYLE_RULES,
  ORDER_NO_PUSHED_COLOR,
  ORDER_TYPE_DIALOG_TITLE,
  REMARK_MAX_LENGTH,
  REMARK_PROMPT_TITLE,
  renderSummaryText,
  ROLE_NAME_MAX_LENGTH,
  ROLE_NAME_PROMPT_TITLE,
  setOrderListPermissions,
} from '../order-orderlist'
// 名单与传输层替身：`assignOrder` / `clearTaker` **保持真身**（断言的是真 `api/orders.ts`
// 拼出来的请求配置，不是页面自造的中间态——E-0009 的同族纪律）。
import { fetchAssigneeOptions } from '@/api/orders'
import { request, requestEnvelope } from '@/api/http'
import { BizError } from '@/api/errors'
import { ORDER_STATUS_LABELS, ORDER_STATUS_VALUES } from '@/config/orderStatus'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

/**
 * 管理订单页第一批（`/orders`）的**减法**：业务方裁定「去掉的控件从页面上**真删**」
 * （不是保留渲染再提示「尚未开放」）。
 *
 * 依据：`docs/specs/2026-09-22-order-list-phase1-design.md` §1（裁定 2 / 8）、§4.1 / §4.2；
 * 决策号 **D-0093**。
 * ⛔ 不要照对标把这些控件「修」回来。
 *
 * ⚠️ 本文件下半部分是**挂载级**用例：`LAYUI` 替身、`mountPage`、`triggerDone` 的形状
 * 照抄 `order-orderlist.spec.ts`（那边有完整出处注释）；这里**只抄用到的**，其余不搬
 * （未使用的桩会被 `@typescript-eslint/no-unused-vars` 判红）。
 */

/** 裁掉的 8 个筛选 `name`（= 7 组控件；`key4Type` 与该组的 `key4` 成对删）。 */
const DELETED_FILTER_NAMES = [
  'gameServer',
  'gameJob',
  'projectType',
  'projectSelect',
  'key4Type',
  'key4',
  'orderLabel',
  'gameAcc',
] as const

/** 裁掉的 15 颗工具栏按钮（`data-tb`）：D-0093 的 12 颗 + 三颗排团按钮。 */
const DELETED_TOOLBAR = [
  'setOrderThrowHomeBtn',
  'cleanOrderThrowHomeBtn',
  'copyToNewOrderBtn',
  'insideOrderPushGroupMsgBtn',
  'setOrderLabelBtn',
  'setJiWei',
  'setOrderTipsBtn',
  'setOrderJiajiBtn',
  'sendSmsBtn',
  'setOrderSoureIdBtn',
  'setOrderProjectBtn',
  'setOrderRemarkBtn',
  'setOrderToTaskBtn',
  'cleanOrderBtTaskBtn',
  'assOrderToTaskMangerBtn',
] as const

/** 工具栏那一组按钮的原文（`.layui-btn-group`；它下面只有 `<button>`，所以第一个 `</div>` 就是它）。 */
const TOOLBAR_GROUP = /<div class="layui-btn-group"[\s\S]*?<\/div>/.exec(EVIDENCE)?.[0] ?? ''

/**
 * 证据件里每个 `class="…"` 拆成**独立 token**（去重前的全量列表）。
 *
 * ⚠️ **必须按 token 比，不能按子串 / `\b` 正则比**：`layui-btn-group`（工具栏那一组的外层 `div`）
 * 里**含子串 `group`**，`/class="[^"]*\bgroup\b/` 这种写法在 `-` 处也算词边界 ⇒ 会连它一起命中
 * （**实测**：删前那条正则在证据件上命中 **5** 行——4 个真 `group` token 行 + 工具栏外壳的
 * `layui-btn-group` 那一行）。而对标用的 `$(".group")` 是**类选择器**、只认独立 token ⇒ 判据同口径。
 */
const CLASS_TOKENS = [...EVIDENCE.matchAll(/class="([^"]*)"/g)].flatMap((m) =>
  m[1]!.trim().split(/\s+/),
)

describe('业务方裁定删掉的控件（D-0093）', () => {
  it('证据件里这 8 个筛选名一个都不剩', () => {
    for (const name of DELETED_FILTER_NAMES) {
      expect(EVIDENCE).not.toContain(`name="${name}"`)
    }
  })

  it('证据件里这 15 颗按钮一个都不剩', () => {
    for (const tb of DELETED_TOOLBAR) expect(EVIDENCE).not.toContain(`data-tb="${tb}"`)
  })

  it('工具栏恰好剩 11 颗按钮（删前 27 颗；**11 颗全是真功能**、`NOT_OPEN_TOOLBAR` 已空）', () => {
    // 数字钉在**证据件本身**上：有人照对标把按钮加回来时，注册表那边不一定会红，这一条会。
    // ⛔ 判据仍是恰好条数，**不**放宽成 `toBeLessThan` 之类。
    expect(TOOLBAR_GROUP).not.toBe('')
    expect([...TOOLBAR_GROUP.matchAll(/data-tb="([^"]+)"/g)].map((m) => m[1])).toHaveLength(11)
  })

  it('⭐ 工具栏「记退款」按钮带 `layui-btn-danger`（2026-10-10 业务方要求醒目；有意偏离，⛔ 别照对标改回默认绿）', () => {
    // 对标原文是 `class="layui-btn layui-btn-sm"`（layui 默认绿）；这是**有意偏离**：
    // 我们仓库里的证据件副本直接加了 `layui-btn-danger`（先例 = D-0140 改 `#project` 的 `maxlength`）。
    // 登记：`docs/FRONTEND-REPLICA-STANDARD.md` §7「已知会不同」+ `order-orderlist.ts` 文件头「已知差异 58」。
    // 本条钉在**证据件本身**上：有人照对标把 class 修回去时，这条会红。
    const m = /<button class="([^"]*)" data-tb="refundOrderBtn"/.exec(TOOLBAR_GROUP)
    expect(m, '证据件里找不到 refundOrderBtn 那颗按钮').not.toBeNull()
    expect(m![1]!.trim().split(/\s+/)).toContain('layui-btn-danger')
  })

  it('⭐ 批量操作线 Task 1 删掉的「同步接单价」在证据件里**零命中**（spec §7-T7；防的是「加回别处」这一档）', () => {
    // ⚠️ 上面那条只数 `layui-btn-group` **组内** ⇒ 抓得住「照对标加回**组里**」，
    //    ⛔ **抓不住「加回别处」**（另一个容器 / 另一组 / 隐藏模板 / 换成 `lay-event` 等形态）。
    // 这里判的是**被废除的主词本身**（裸名 `asyncHomeOrderPriceBtn`，⛔ 不是 `data-tb="…"` 这个派生
    // 形状）⇒ 它出现在证据件的**任何位置、任何形态**都会红（E-0011 同族：只搜派生短语会给出假保证）。
    // ⛔ 不许放宽成 `toContain` / `toMatch` 之类；⛔ 不许只留注释不落判据。
    // 📌 spec §7-T7 点名的是**两半**：`data-tb` **总数** + 这个裸名**零命中**——两半都已落地
    //    （本条 + 下面那条「总数恰好 12」），⛔ 别把下面那条当成超范围。
    expect(EVIDENCE).not.toContain('asyncHomeOrderPriceBtn')
  })

  it('⭐ 证据件里 `data-tb` **总数恰好 12**（组内 11 + 组外「搜索」`seachBtnOk` 1；spec §7-T7）', () => {
    // 上面那条只数工具栏**组内** ⇒ 组外凭空多出一颗 `data-tb` 时它不会红。这条按**全文**数：
    // 对标原件 13（组内 12 + 组外 1），业务裁定删 1 ⇒ **12**（判据是恰好条数，⛔ 不放宽成上下限）。
    // 它抓的是「删掉的那些名字之外」也**不许再冒出**第 13 颗（`DELETED_TOOLBAR` 那份名单只认名字，
    // 认不出一个全新名字）。
    // ⚠️ **代价（如实登记）**：它钉的是**全文**恰好条数 ⇒ 将来**合理地**增删一颗
    // `data-tb`（业务方再裁定删一颗按钮、或给本页加一颗新按钮）也会让它红。那是有意的：改证据件时
    // **同一次**把这里的 12 一并更新（与本仓其它「恰好集合」判据同一纪律），⛔ 不许把它放宽成上下限。
    expect([...EVIDENCE.matchAll(/data-tb="([^"]+)"/g)].map((m) => m[1])).toHaveLength(12)
  })

  it('删掉的 5 个节点一个都不剩，而工具栏那一组的外壳（`layui-btn-group`）不受影响（⛔ 两条独立的链）', () => {
    // ⛔ 判据搜的是**被废除的主词本身**（`class` 里独立的 `group` / `task` 两个 token），不是
    //    「gameGroup / taskSelect」这两个派生名字——只搜派生名会给出一份**假保证**（E-0011 同族）。
    // **实测 token 计数**（`CLASS_TOKENS`，按独立 token 数）：`group` 删前恰好 **4** 处
    //    （阵营筛选块 1 + 三颗排团按钮 3）、`task` 删前恰好 **1** 处（排团团号筛选块）⇒ 删后都应为 0。
    expect(CLASS_TOKENS).not.toContain('group')
    expect(CLASS_TOKENS).not.toContain('task')
    // 两个控件名本身也不该残留（整块删，不是只删内层 `<select>`）。
    expect(EVIDENCE).not.toContain('name="gameGroup"')
    expect(EVIDENCE).not.toContain('name="taskSelect"')
    // 另一条链仍在：`layui-btn-group` 是**另一个** token（工具栏那组的外壳），
    // `$(".group")` 命不中它 ⇒ 它必须还在（上面 `TOOLBAR_GROUP` 的取法也依赖它）。
    expect(CLASS_TOKENS).toContain('layui-btn-group')
    // ⚠️ `_group`（「阵营」列）已在第二批列改动（D-0094，见 `order-orderlist.ts` 文件头
    //    「已知差异 22」）里删掉 ⇒ 证据件里
    //    既没有 `#col_group` 也没有 `field: '_group'`。
    //    故这里**不写**「列链仍在」的断言——写了与实测不符（就是假话）。
  })

  it('行内的单笔「发到大厅 / 撤回」仍然在（业务方：单个发即可）', () => {
    // ⚠️ 这两颗**不在证据件里**：证据件是静态 HTML，行内按钮由页面第 28 列的真 templet
    // （`buildOrderCols` 的 `btnfun`）**运行期**渲染 ⇒ 判据必须打在 templet 的产物上。
    // （brief 的参考片段把这两条写成 `expect(EVIDENCE).toContain(...)`，那在本仓库**恒红**：
    //  改前改后 `EVIDENCE` 里都没有这两个 class 名——本用例按同一意图改写。）
    // ⚠️ 这两颗也按节点渲染：先显式授权，否则默认 fail-closed 会把它们挡掉
    // （那是**门**的行为，不是「业务方删了它们」）。
    setOrderListPermissions({
      // ⭐ 发大厅判**独占**节点（20 只管改发单价、不再开这道门）。
      menu_permissions: ['order.send_to_grab_hall', 'order.withdraw_from_grab_hall'],
    })
    const row = {
      order_no: '20260920213215233029',
      source_order_no: '',
      status: 'UNASSIGNED',
      game: 2,
      server: null,
      project_text: 'x',
      total_price: '1.00',
      customer_phone: '',
      is_urgent: false,
      created_at: '2026-09-20T21:32:15+08:00',
    }
    const cell = buildOrderCols(
      () => 'G',
      () => '',
    )[22]!.templet as (d: typeof row) => string
    expect(cell({ ...row, status: 'UNASSIGNED' })).toContain('throwEdit')
    expect(cell({ ...row, status: 'WAITING_GRAB' })).toContain('cancelThrowHome')
  })
})

/* ═══════════ 指派打手 / 清除接单人（含「接手人」列三态） ═══════════ */

/**
 * layui 的**可控替身**（挂载整页用）。形状照 `order-orderlist.spec.ts`，**只留本文件用到的成员**：
 * `bootstrapIsland` 在 jsdom 里加载不了真 layui，换成这份替身后 `layer.open` / `layer.msg` /
 * `layer.confirm` 的**入参**才能被断言。
 */
const LAYUI = vi.hoisted(() => {
  const open = vi.fn()
  const msg = vi.fn()
  const confirm = vi.fn()
  /**
   * 真 layui 的层登记表：真 `layer.open({content})` 会把节点
   * **搬进 `.layui-layer-content`**（`root` 之外），`layer.close(index)` 再触发该层的 `end`
   * 并销毁层 DOM。替身照这个形状搬节点 ⇒ `settPanel()` 的文档级分支与 `end()` 的
   * 「搬回 host」两条分支靠它覆盖（判别力见本文件「真 layui 搬节点」那条用例）。
   */
  const layers = new Map<number, { node: HTMLElement | null; end?: () => void }>()
  let nextIndex = 1
  /** ⭐ T1 的 loading 层号**定值哨兵**（见下面 `layer.load` 的说明）。 */
  const loadIndex = 9002
  /** 真 layui 形状的 `layer.open`：**搬节点** + 调 `success` + 返回层索引。 */
  const realOpen = (opts: unknown): number => {
    const cfg = (opts ?? {}) as {
      content?: unknown
      success?: (layero: HTMLElement, index: number) => void
      end?: () => void
    }
    const index = nextIndex++
    const layer = document.createElement('div')
    layer.className = 'layui-layer'
    const content = document.createElement('div')
    content.className = 'layui-layer-content'
    layer.appendChild(content)
    document.body.appendChild(layer)
    const node = cfg.content instanceof HTMLElement ? cfg.content : null
    if (node) content.appendChild(node)
    layers.set(index, { node, end: cfg.end })
    cfg.success?.(layer, index)
    return index
  }
  return {
    open,
    realOpen,
    msg,
    confirm,
    form: { render: vi.fn(), on: vi.fn(), val: vi.fn(() => ({})) },
    table: {
      render: vi.fn(),
      on: vi.fn(),
      reload: vi.fn(),
      // 返回类型显式写成 `{ data: unknown[] }`：否则推断成 `never[]`，
      // 用例里的 `mockReturnValue({ data: [ROW] })` 过不了类型检查。
      checkStatus: vi.fn((): { data: Array<{ order_no: string } & Record<string, unknown>> } => ({ data: [] })),
      getOptions: vi.fn((): { page: { limit: number } } => ({ page: { limit: 20 } })),
    },
    layer: {
      open,
      msg,
      // 真 layui：先把层 DOM 拆掉（节点随之脱开文档），**再**调 `end`（`end` 里把节点搬回 host）。
      close: vi.fn((index: number) => {
        const rec = layers.get(index)
        if (rec === undefined) return
        layers.delete(index)
        rec.node?.closest('.layui-layer')?.remove()
        rec.end?.()
      }),
      /**
       * ⭐ T1：`runBatchFlow` 现在会 `layer.load(1, { shade: false })` + `layer.close(index)`。
       * 层号用**定值哨兵**（与 `open` 的递增层号不冲突，也不在 `layers` 表里 ⇒ `close` 的既有行为
       * 对它无害）。本文件既有的「`close(3)`」类判据表达的都是**弹层**语义 ⇒ 关 loading 那一发
       * 要过滤掉（见 `dialogCloses()`）。
       */
      load: vi.fn(() => loadIndex),
      prompt: vi.fn(),
      tips: vi.fn(),
      confirm,
      alert: vi.fn(),
    },
    laydate: { render: vi.fn() },
    // `layer.open({content: layui.$(node)})` 要一个 jQuery 形状的对象（本文件不点发价弹层，
    // 但挂载路径与它同源，补上是零成本的保险）。
    $: (el: Element) => el,
    element: { init: vi.fn() },
    /** ⭐ T1 的 loading 哨兵层号（既有断言里过滤它用）。 */
    loadIndex,
    /** 每个用例前清空层登记表（`open.mockReset()` 抹掉实现后要重装 `realOpen`）。 */
    __resetLayers: (): void => {
      layers.clear()
      nextIndex = 1
    },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

/**
 * ⚠️ 必须有这一条：页面接真批量后走 `@/layui-island/orderBatch.ts`
 * 的 `runBatchFlow`，而它取 `layer` 的出口是 `@/layui/loader` 的 `loadLayui(['layer'])`
 * （⛔ **不是** `bootstrapIsland`）。jsdom 里真 `loadLayui` 会去插 `<script>` 并失败 ⇒ 批量分支
 * 会「报一句 + 返回 false、一发请求都不发」——那会把新判据伪装成「功能没接上」。
 * 指到同一个 `LAYUI` 替身即可（与 `bootstrapIsland` 替身同源）。
 */
vi.mock('@/layui/loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/loader')>()
  return { ...actual, loadLayui: async () => LAYUI }
})

/**
 * ⭐ T1：只看**弹层**的 `layer.close` 调用参数（过滤掉 `runBatchFlow` 关 loading
 * 用的哨兵层号）。本文件既有那些「成功才关层 / 失败不关层」判据表达的都是**弹层**语义
 * ⇒ 一律改用它；loading 自己的开/关由 `order-orderlist-batch.spec.ts` 的 T1/T2 组断言。
 */
function dialogCloses(): number[] {
  return LAYUI.layer.close.mock.calls
    .map((call) => call[0])
    .filter((index) => index !== LAYUI.loadIndex)
}

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

/**
 * ⚠️ **T9 起**本页 mount 时会取候选名单（`fetchFilterOptions`）与订单来源字典
 * （`fetchOrderSources`）各一发。两个都替身化：本文件多处断言「这次动作发了几发 `request()`」，
 * 那两发 GET 会污染计数（**判据本身不动**）。候选名单那一面在 `order-orderlist-filters.spec.ts`。
 */
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
    // 指派弹层的候选名单（D-0146 方案②）：换到 `orders/assignee-options/` 之后，替身挂在
    // **真出口** `fetchAssigneeOptions` 上（页面调的就是它，不是页面自造的中间变量）。
    fetchAssigneeOptions: vi.fn(),
  }
})

vi.mock('@/api/orderSources', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orderSources')>()
  return {
    ...actual,
    fetchOrderSources: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 200 })),
    // ⭐ `mount` 会多取一次分组字典（筛选栏「订单分组」的候选）
    // ⇒ 不替身化会多一发真请求（本文件多处用 `mockResolvedValueOnce` 排队响应 ⇒
    //   队首被它吃掉，批量动作读到错形状，`[orderBatch] … length` 当场红）。
    fetchOrderSourceGroups: vi.fn(async () => ({
      items: [],
      total: 0,
      page: 1,
      page_size: 200,
    })),
  }
})

/**
 * 传输层替身：`request()` 被换掉，**`assignOrder` / `clearTaker` 保持真身**——「打的是哪个 URL、
 * 载荷是什么」因此断言在真 `api/orders.ts` 的产物上（见文件顶部 import 处的说明）。
 *
 * ⚠️ **工厂必须是「不 importOriginal」的形态**（原因见下）：
 * `@/api/http` 与 `@/api/auth` 之间有一处**有意为之的循环依赖**（`http.ts` 的 `fetchCsrf` ↔
 * `auth.ts` 的 `request`）。写 `async (importOriginal) => ({...actual, request: vi.fn()})` 会让
 * 真 `http.ts` 被实例化，`auth.ts` 的 `request` 绑定因而落在**真身**上、换不掉 ⇒ 页 mount 里
 * `fetchMe()` 打真网络（实测 `NetworkError: 网络异常`）、行内权限门整片 false，
 * 「指派打手 / 订单截图」的挂载级用例会看到**空入口**。
 * `http` 也给一个替身：`@/api/performances` 的导出用 `http.get`（本文件不驱动它，但模块求值要能过）。
 * 先例：`order-myorderlist.spec.ts` 用的就是同一形状的同名替身。
 */
vi.mock('@/api/http', () => ({
  request: vi.fn(),
  // ⭐ 指派成功要读后端 `msg`（可能带「未能撤回」警告）⇒ 页面走 `requestEnvelope`。
  // 默认把它转接到 `request` 替身（既有用例仍只配 `request` 就能驱动全流程）。
  requestEnvelope: vi.fn(),
  http: { get: vi.fn() },
}))

/** 一行真实形状的订单列表载荷（字段集 = `OrderListSerializer`，含本轮新增的 `taker_name`）。 */
const ROW = {
  order_no: '20260920213215233029',
  source_order_no: '',
  source: '',
  status: 'UNASSIGNED',
  game: 1,
  server: null,
  project_text: '原神-深渊',
  total_price: '100.00',
  refund_fee: '0.00',
  customer_phone: '13800000000',
  is_urgent: false,
  created_at: '2026-09-22T10:00:00+08:00',
  taker_name: '',
  // ⭐ 后端布尔键（占位接手人 ⇒ 接手人那格转紫）。
  // ⚠️ 显式写成 `boolean | undefined`：默认 `undefined` = 键缺失（后端未升级 / 大厅载荷），
  //    用例要分别覆盖 `true` / `false` / 缺失三档；只写 `undefined` 会让字面量类型锁死。
  taker_is_platform: undefined as boolean | undefined,
  // ⚠️ 显式写成 `string | null`（后端 `OrderListItem.hall_price` 就是这个类型）：默认值虽然是
  //    `null`，但 B12 的用例要把它覆盖成金额串；只写 `null` 会让字面量类型锁成 `null`。
  hall_price: null as string | null,
  hall_publisher_name: '',
  payment_status: 'UNSETTLED',
  payment_method: '',
}

/**
 * `fetchAssigneeOptions` 的替身返回值：**两个**代练员（`id` 是 `/assign/` 要的 `user_id`）。
 *
 * ⚠️ 形状 = 专用端点的 `{items, truncated, roles}`（D-0147 加第三个键），每项恰好
 * `{id,name,role,tag,remark}`（**`name` 已由后端算好**：昵称优先、空串回落登录账号——
 * 页面不再自己拼 `display_name||username`）。
 * ⚠️ T6 起要两个：单人版指派弹层的核心判据之一是「勾第二个 ⇒ 提示且**不改变**已有选择」，
 * 一个账号验不出这一支（扩夹具不是放宽判据）。`role` / `tag` / `remark` 是多选列表的
 * `lay-tips` 与徽标读的字段。
 * ⚠️ `roles` 是**合成值**（E-0077）、也是「用户职位」那一组的**唯一来源**：
 * 它们刻意**不**等于任何硬编码内置名单，这样「页面渲染的是接口返回值」才验得出来；
 * 里面留着「代练员」是为了让默认档（`ASSIGN_ROLE_DEFAULT`）命中、不触发 V-G 的回退。
 */
const ACCOUNTS = {
  items: [
    { id: 7, name: '打手一', role: '代练员', tag: '', remark: '' },
    { id: 8, name: '打手二', role: '代练员', tag: '', remark: '' },
  ],
  truncated: false,
  roles: ['测试职位甲', '代练员', '测试职位乙'],
}

/**
 * U7 的第二档夹具：`roles` 里**没有**「代练员」⇒ 默认档静默回退到「全部」（V-G）。
 * 三个名字都是合成的自建职位（E-0077）。
 */
const ACCOUNTS_WITHOUT_TAKER_ROLE = {
  ...ACCOUNTS,
  roles: ['测试职位甲', '测试职位乙', '测试职位丙'],
}

/**
 * `GET /api/business/orders/` 响应 `data.summary` 的真实形状（spec §7.1）——
 * 金额是**两位小数字符串**，`status_counts` 覆盖 8 个状态。
 */
const SUMMARY = {
  count: 12,
  total_hall_price: '3600.00',
  total_price: '4800.00',
  total_refund_fee: '180.00',
  diff: '1200.00',
  refunded_diff: '150.00',
  status_counts: {
    UNASSIGNED: 3,
    WAITING_GRAB: 0,
    IN_PROGRESS: 4,
    PENDING_REVIEW: 1,
    COMPLETED: 4,
    ISSUE: 0,
    PAUSED: 0,
    REFUNDED: 2,
  },
}

/**
 * 证据件里 `.total` span 的**原文**
 * （所有数字都是 `0` / `0.00`）。挂载级用例先用它证明「灌进渲染工具栏的确实是模板原文」。
 */
const SUMMARY_ZERO_TEXT =
  '本次搜索统计共0笔，预计总发单价0.00元(不含接单提成)，总接单价0.00元，' +
  '预计差额0.00元。其中：未分配0笔，待抢单0笔，代练中0笔，待验收0笔，' +
  '已完成0笔，已暂停0笔，问题单0笔，已退单0笔'

/**
 * 期望的那句：证据件原句 + **业务方 2026-11-05 拍板的改版**（对标偏离，登记在案）：
 * 「预计差额」改叫「预计利润」、前面加「总退款X元」段、括号补「含已退单部分Y元」（Y = 后端
 * `refunded_diff`）与「含已退款部分Z元」（Z = `total_refund_fee`，业务方 2026-11-07）。
 * 只把数字换成 `SUMMARY` 的值。
 *
 * ⚠️ 状态段顺序 = **证据件那句的顺序**，而它与 `ORDER_STATUS_VALUES` 的**声明顺序不同**
 * （声明顺序里 `ISSUE` 在 `PAUSED` 前）。值纪律以证据件为准（见 `renderSummaryText` 的注释）。
 */
const SUMMARY_TEXT =
  '本次搜索统计共12笔，预计总发单价3600.00元(不含接单提成)，总接单价4800.00元，' +
  '总退款180.00元，预计利润1200.00元(含已退单部分150.00元、含已退款部分180.00元)。其中：未分配3笔，待抢单0笔，代练中4笔，待验收1笔，' +
  '已完成4笔，已暂停0笔，问题单0笔，已退单2笔'

/**
 * `renderSummaryText` 的产物是**标记**（每个状态段是一个
 * `<span class="statustab">`）⇒ 要断言「给用户看的那整句话」时先把它当 HTML 解析、读 `textContent`。
 * ⛔ 这不是放宽断言：整句仍与 `SUMMARY_TEXT` **逐字**比（只是把标记剥掉）；标记本身由上一条用例钉。
 */
function summaryText(html: string): string {
  const host = document.createElement('div')
  host.innerHTML = html
  return host.textContent ?? ''
}

/** `request()` 替身收到的**真请求配置**（经真 `assignOrder` / `clearTaker`）。 */
function lastRequestConfig(): { method?: string; url?: string; data?: unknown } {
  const calls = vi.mocked(request).mock.calls
  return (calls[calls.length - 1]?.[0] ?? {}) as { method?: string; url?: string; data?: unknown }
}

describe('「接手人」列的三态（C5 + §9：脱敏值是后端给的哨兵，前端不镜像权限）', () => {
  /**
   * 第 11 列（下标 10）「接手人」的真实 templet。
   * ⛔ 原先写「第 15 列（下标 13）」——那是 28 列时的位置；2026-09-23 第二批列删 4 · 去重 1 之后
   * 它是 `buildOrderCols()` 的第 11 列、下标 10（判据：`order-orderlist.spec.ts` 的列集合数组，
   * `cols[10]` 即 `field: '_rec'`）。spec §9.1 #19 登记的就是这处。
   * ⚠️ 三态是 binding：`''` → 「指派打手」按钮、非空 → 打手名字、`无权查看` → **纯文本**。
   */
  const takerTemplet = (): ((d: typeof ROW) => string) => {
    const cols = buildOrderCols(
      () => '原神',
      () => '',
      { isGroupGame: () => false },
    )
    const taker = cols.find((c) => c.title === '接手人')
    expect(taker, '列定义里找不到「接手人」').toBeDefined()
    return taker!.templet as (d: typeof ROW) => string
  }

  /**
   * ⚠️ **权限门默认 fail-closed**（本线 minimal-role-audit）：模块级**六个**开关默认全 false ⇒
   * 不先授权的话，下面的三态用例会看到「入口被门挡掉」而不是三态本身。本组测的是**有**
   * `order.assign_taker` 时的三态，所以先显式授权；**没有**权限那一支另有专门的用例
   * （见文件末尾「权限门」一组）。
   */
  beforeEach(() => {
    setOrderListPermissions({ menu_permissions: ['order.assign_taker'] })
  })

  it('没人接 ⇒ 「指派打手」按钮（class / 文案逐字照旧）', () => {
    const html = takerTemplet()({ ...ROW, taker_name: '' })
    expect(html).toContain(
      'class="layui-btn layui-btn-xs layui-btn-normal jsr"',
    )
    expect(html).toContain('>指派打手</a>')
  })

  it('有人接 ⇒ 渲染那个名字（同一个 `jsr` 链接，点击 = 改派；**不是**按钮）', () => {
    const html = takerTemplet()({ ...ROW, taker_name: 'taker1' })
    expect(html).toContain('taker1')
    expect(html).toContain('jsr')
    // 对标 `recNameRow` 三档里**只有没人接**那一档是按钮 ⇒ 名字这档不带 `layui-btn` 类
    expect(html).not.toContain('layui-btn')
    expect(html).not.toContain('指派打手</a>')
  })

  it('「无权查看」⇒ 纯文本，**不带** `jsr`（无权进指派界面，那一格不可点）', () => {
    const denied = takerTemplet()({ ...ROW, taker_name: '无权查看' })
    expect(denied).toContain('无权查看')
    expect(denied).not.toContain('jsr')
  })

  /**
   * T8 / MF-1（安全级）：T4 起 `taker_name` 是**昵称优先**（`first_name or username`），
   * 而昵称是**可编辑自由文本** ⇒ 这是活的存储型 XSS。
   *
   * 判据（spec §8 第 29 条）：属性（`data-taker`）与正文**两处**都必须过 `escapeHtml`。
   * 夹具里带一个 `"`（证明属性上下文也没被破坏）与一个 `<img>`（证明正文没被当标记）。
   */
  it('昵称是自由文本 ⇒ 属性与正文两处都转义（spec §8 第 29 条 / C11 / T8 MF-1）', () => {
    const evil = 'a"b<img src=x>'
    const html = takerTemplet()({ ...ROW, taker_name: evil })
    // 标记里没有裸 `<img`（转义后是 `&lt;img`）
    expect(html).not.toContain('<img')
    // 两处插值（`data-taker` 属性与正文）都必须是转义后的串
    const hits = html.match(/a&quot;b&lt;img src=x&gt;/g) ?? []
    expect(hits).toHaveLength(2)
    // 属性那一处：引号也被转义（否则 `"` 会提前闭合属性）
    expect(html).toContain('data-taker="a&quot;b&lt;img src=x&gt;"')
    expect(html).not.toContain('data-taker="a"b')
  })

  /* ⭐「接手人」列对**代练通占位账号**转紫。
   *
   * 判据是后端布尔键 `taker_is_platform`（按占位账号 username 判），⛔ 不是前端比名字串；
   * 色值复用订单号那支紫 `ORDER_NO_PUSHED_COLOR`（`#D15FE8`，⛔ 不新写死色值）。 */

  it('真接手人 ⇒ 默认色（没有紫色）', () => {
    const html = takerTemplet()({ ...ROW, taker_name: 'taker1', taker_is_platform: false })
    expect(html).toContain('taker1')
    expect(html).not.toContain(ORDER_NO_PUSHED_COLOR)
    expect(html).not.toContain('#D15FE8')
  })

  it('占位接手人（taker_is_platform === true）⇒ 文字紫色，色值复用订单号那支紫', () => {
    const html = takerTemplet()({
      ...ROW,
      taker_name: '代练通',
      taker_is_platform: true,
    })
    expect(html).toContain('>代练通</a>')
    // 钉**值**（不是只钉常量）：色值必须是 #D15FE8，且与既有紫色常量同源。
    expect(html).toContain('color:#D15FE8')
    expect(ORDER_NO_PUSHED_COLOR).toBe('#D15FE8')
    expect(html).toContain(`color:${ORDER_NO_PUSHED_COLOR}`)
  })

  it('键缺失（后端未升级 / 大厅载荷）⇒ 默认色，⛔ 不炸', () => {
    const html = takerTemplet()({ ...ROW, taker_name: '代练通' })
    expect(html).toContain('代练通')
    expect(html).not.toContain('#D15FE8')
  })
})

/* ═══════════ 权限门（没节点就不渲染入口；D-0154 的同一原则） ═══════════ */

/**
 * 为什么要有这一组：职位对拍网新增了一个**自定义最小权限角色**（只给 `order.list`），它打开了
 * `/orders` ⇒ 行内那几组入口原来**无条件渲染**，点下去必然打 403（`assignee-options/`、
 * `/screenshots`、`GET /orders/<no>/`，见 spec §2 与 `D-0154`）。处置照 `D-0154`：
 * **按节点渲染、fail-closed**。
 *
 * ⛔ 判据必须打在**真实 templet 的产物**上（`buildOrderCols` 取列、调它的 `templet`），
 * 不是打在 `setOrderListPermissions` 的入参上——后者证明不了「渲染时读了它」。
 *
 * ⭐ **评审修复波扩了三条轴**：
 *  1. **必修 2 —— 元组门取 any**：指派入口的门是 `assignee_options` 那组
 *     any(`assign_taker`, `submit_payout_price`, `clear_taker`) ⇒ 三个节点**各自**都要能单独放行
 *     （只按 `assign_taker` 单点会**过度隐藏**）。
 *  2. **必修 1 —— 编辑订单的门取弹层第一发**：门是 `GET /orders/<no>/`（retrieve 的
 *     any(`view_detail`, `view_content`)），⛔ **不是**保存那半 `add_edit`。只给 `add_edit` 时
 *     仍然不渲染（点开必 403）。
 *  3. **必修 3 —— `.throwEdit` / `.cancelThrowHome` 也按节点渲染**：前者 any(`set_offer_price`,
 *     `send_to_grab_hall`, `submit_payout_price`)、后者 `withdraw_from_grab_hall`；两颗的门**不同**，
 *     互不串门。
 */
describe('行内入口的权限门：没有节点就不渲染（本线 minimal-role-audit；评审修复波：元组门取 any）', () => {
  const colsOf = (): ReturnType<typeof buildOrderCols> =>
    buildOrderCols(() => '原神', () => '', { isGroupGame: () => false })

  /** 「接手人」列的真实 templet（按 title 取，⛔ 不按下标——下标会随删列漂移）。 */
  const takerTemplet = (): ((d: typeof ROW) => string) => {
    const taker = colsOf().find((c) => c.title === '接手人')
    expect(taker, '列定义里找不到「接手人」').toBeDefined()
    return taker!.templet as (d: typeof ROW) => string
  }
  /** 「订单截图」列的真实 templet。 */
  const imgTemplet = (): ((d: typeof ROW) => string) => {
    const img = colsOf().find((c) => c.title === '订单截图')
    expect(img, '列定义里找不到「订单截图」').toBeDefined()
    return img!.templet as (d: typeof ROW) => string
  }
  /** 「操作」列的真实 templet（末列，`.submitEdit` / `.throwEdit` / `.cancelThrowHome`）。 */
  const opTemplet = (d: typeof ROW = ROW): string => {
    const op = colsOf().find((c) => c.title === '操作')
    expect(op, '列定义里找不到「操作」').toBeDefined()
    return (op!.templet as unknown as (r: typeof ROW) => string)(d)
  }
  /** 池内那笔（`WAITING_GRAB`）——末列那颗换成 `.cancelThrowHome`。 */
  const WAITING_ROW = { ...ROW, status: 'WAITING_GRAB' }

  it('指派入口：三个节点**任一**命中即渲染（any 并集；⛔ 不取单点——单点会过度隐藏）', () => {
    for (const node of ['order.assign_taker', 'order.submit_payout_price', 'order.clear_taker']) {
      setOrderListPermissions({ menu_permissions: [node] })
      const html = takerTemplet()({ ...ROW, taker_name: '' })
      expect(html, `只给 ${node} 时「指派打手」被藏掉了（过度隐藏，评审必修 2）`).toContain(
        '指派打手',
      )
    }
  })

  it('指派入口：三个节点一个都没有 ⇒ 空态不渲染按钮；有人接也只出纯文本（不带 `jsr`）', () => {
    setOrderListPermissions({ menu_permissions: [] })
    const templet = takerTemplet()
    expect(templet({ ...ROW, taker_name: '' })).toBe('')
    const named = templet({ ...ROW, taker_name: 'taker1' })
    expect(named).toContain('taker1')
    expect(named).not.toContain('jsr')
    expect(named).not.toContain('layui-btn')
  })

  it('身份取不到（`undefined`）⇒ 行内各道门全部 fail-closed（含末列两颗写入口）', () => {
    setOrderListPermissions(undefined)
    expect(takerTemplet()({ ...ROW, taker_name: '' })).toBe('')
    expect(imgTemplet()(ROW)).toBe('')
    expect(opTemplet()).not.toContain('submitEdit')
    expect(opTemplet()).not.toContain('throwEdit')
    expect(opTemplet(WAITING_ROW)).not.toContain('cancelThrowHome')
  })

  it('租户管理员短路 ⇒ 指派按钮 / 两颗截图 / 编辑 / 发大厅 / 撤回全在', () => {
    setOrderListPermissions({ is_tenant_admin: true, menu_permissions: [] })
    expect(takerTemplet()({ ...ROW, taker_name: '' })).toContain('指派打手')
    const img = imgTemplet()(ROW)
    expect(img).toContain('upImg')
    expect(img).toContain('mangeImg')
    expect(opTemplet()).toContain('submitEdit')
    expect(opTemplet()).toContain('throwEdit')
    expect(opTemplet(WAITING_ROW)).toContain('cancelThrowHome')
  })

  it('订单截图：只有 view ⇒ 只出 mangeImg；只有 upload ⇒ 只出 upImg；都没有 ⇒ 空串（不留空壳）', () => {
    setOrderListPermissions({ menu_permissions: ['order.view_screenshot'] })
    const viewOnly = imgTemplet()(ROW)
    expect(viewOnly).toContain('mangeImg')
    expect(viewOnly).not.toContain('upImg')
    setOrderListPermissions({ menu_permissions: ['order.upload_screenshot'] })
    const uploadOnly = imgTemplet()(ROW)
    expect(uploadOnly).toContain('upImg')
    expect(uploadOnly).not.toContain('mangeImg')
    setOrderListPermissions({ menu_permissions: [] })
    expect(imgTemplet()(ROW)).toBe('')
  })

  it('编辑订单：门取**弹层第一发** `GET /orders/<no>/`（any 详情节点），⛔ 不是保存那半 `add_edit`', () => {
    // 只给 `add_edit`（PATCH 那半）⇒ 弹层仍会 403（第一发是详情 GET）⇒ **不渲染**。
    setOrderListPermissions({ menu_permissions: ['order.add_edit'] })
    expect(
      opTemplet(),
      '只给 order.add_edit 就渲染了「编辑订单」——弹层第一发是 GET /orders/<no>/，点开必 403',
    ).not.toContain('submitEdit')
    // 两个详情节点任一即可（后端 retrieve 是 any 元组）。
    for (const node of ['order.view_detail', 'order.view_content']) {
      setOrderListPermissions({ menu_permissions: [node] })
      expect(opTemplet(), `只给 ${node} 时「编辑订单」被藏掉了`).toContain('submitEdit')
    }
  })

  it('发大厅 = 36 独占；撤回单独一颗（两颗的门**不同**、互不串门）', () => {
    // 都没有 ⇒ 两颗都不渲染（审批必修 3 之前它们是**无条件**渲染的）。
    setOrderListPermissions({ menu_permissions: [] })
    expect(opTemplet()).not.toContain('throwEdit')
    expect(opTemplet(WAITING_ROW)).not.toContain('cancelThrowHome')
    // ⭐ 发大厅判**独占**节点 ⇒ 只给 `order.send_to_grab_hall` 才渲染那颗。
    setOrderListPermissions({ menu_permissions: ['order.send_to_grab_hall'] })
    expect(opTemplet(), '只给 order.send_to_grab_hall 时「发到内部接单大厅」被藏掉了').toContain(
      'throwEdit',
    )
    // 反向：`order.set_offer_price` / `order.submit_payout_price` **不再**开这道门。
    // 如果前端还在 any 三选一里，这两条会渲染一颗点了必 403 的按钮（D-0154）。
    for (const node of ['order.set_offer_price', 'order.submit_payout_price']) {
      setOrderListPermissions({ menu_permissions: [node] })
      expect(
        opTemplet(),
        `只给 ${node} 就渲染了「发到内部接单大厅」——后端已改判独占 36，点了必 403`,
      ).not.toContain('throwEdit')
      expect(
        opTemplet(WAITING_ROW),
        `${node} 不该让「撤回」那颗出现（两颗的门不同）`,
      ).not.toContain('cancelThrowHome')
    }
    // 撤回：只认 `withdraw_from_grab_hall`；它不反向放行发大厅。
    setOrderListPermissions({ menu_permissions: ['order.withdraw_from_grab_hall'] })
    expect(opTemplet(WAITING_ROW)).toContain('cancelThrowHome')
    expect(opTemplet(), '只给 withdraw 节点不该让「发到大厅」出现').not.toContain('throwEdit')
  })
})

/* ═══════════ Task 8：工具栏统计值（spec §7.1 / §7.2；决策 C17） ═══════════ */

describe('工具栏统计值 renderSummaryText（spec §7.2）', () => {
  it('句子是证据件原句的业务方改版（2026-11-05 利润口径），只把数字换成实值（8 个状态同顺序同中文）', () => {
    // 证据件原句之上：总退款段 / 预计利润 / 含已退单部分 / 含已退款部分（对标偏离，业务方拍板）；
    // 标记只加在状态段上，其余不增删一个字。
    expect(summaryText(renderSummaryText(SUMMARY))).toBe(SUMMARY_TEXT)
  })

  it('状态段带可点标记（class / data-status=状态码 / style），且 8 段一个不少', () => {
    // 对标**运行期**那句的状态段是可点的 `span.statustab` + `lay-tips`
    // （**不是**我们那份静态证据件——里面没有 statustab）。业务方要求照此做成可点（文件头「已知差异 57」）。
    // ⛔ 断言不许放宽成 `toContain('未分配')` 这种松断言：标记的每一项都要在位。
    const text = renderSummaryText(SUMMARY)
    expect(text).toContain(
      '<span style="cursor:pointer" class="statustab" data-status="UNASSIGNED" ' +
        'lay-tips="点击可以快速筛选未分配的订单" title="点击可以快速筛选未分配的订单">未分配3笔</span>',
    )
    expect(text.match(/class="statustab"/g)).toHaveLength(8)
    // `data-status` 用**状态码**（中文由 `statusLabel()` 现算 ⇒ ⛔ 不把中文塞进属性）。
    for (const status of ORDER_STATUS_VALUES) {
      expect(text).toContain(`data-status="${status}"`)
    }
    expect(text).not.toContain('data-status="未分配"')
  })

  it('状态段的 `lay-tips` / `title` 用**我们的**状态名（照对标那句的形状，不抄「待上号」）', () => {
    // 原句形状逐字照对标（`点击可以快速筛选X的订单`），X 由 `statusLabel()` 现算 ⇒ 「待抢单」
    //（对标的「待上号」见文件头「已知差异 12」）。
    const text = renderSummaryText(SUMMARY)
    expect(text).toContain(
      '<span style="cursor:pointer" class="statustab" data-status="WAITING_GRAB" ' +
        'lay-tips="点击可以快速筛选待抢单的订单" title="点击可以快速筛选待抢单的订单">待抢单0笔</span>',
    )
    expect(text).not.toContain('待上号')
  })

  it('笔数 / 金额进了 HTML ⇒ 动态值过 `escapeHtml`（含自由文本形状的恶意值）', () => {
    // `renderSummary` 用 `innerHTML` 落这串 ⇒ 后端聚合值即使
    // 带标记也不许被当 HTML 解释（spec §8 的 escape 纪律）。
    const text = renderSummaryText({ ...SUMMARY, total_price: '<img src=x>' })
    expect(text).not.toContain('<img')
    expect(text).toContain('总接单价&lt;img src=x&gt;元')
  })

  it('@/config/orderStatus 声明的 8 个状态一个不少（漏一个就红）', () => {
    // 反向覆盖：只盯证据件那句话会漏掉「后端加了状态/前端漏渲染」；这条拿**声明表**当清单。
    const text = renderSummaryText(SUMMARY)
    for (const status of ORDER_STATUS_VALUES) {
      expect(text, `统计句里缺了 ${status} 的中文`).toContain(ORDER_STATUS_LABELS[status])
    }
  })

  it('每个状态取自己的计数（8 个值互不相同）', () => {
    // ⚠️ `SUMMARY` 里 `ISSUE`/`PAUSED` 等几档都是 0，只拿它一条验不出「两个计数读反」；
    // 这里给 8 个互不相同的值把**逐状态映射**钉住。
    const text = renderSummaryText({
      ...SUMMARY,
      status_counts: {
        UNASSIGNED: 11,
        WAITING_GRAB: 12,
        IN_PROGRESS: 13,
        PENDING_REVIEW: 14,
        COMPLETED: 15,
        ISSUE: 16,
        PAUSED: 17,
        REFUNDED: 18,
      },
    })
    expect(summaryText(text)).toContain('未分配11笔，待抢单12笔，代练中13笔，待验收14笔')
    // 证据件顺序是「已暂停…问题单」（与 `ORDER_STATUS_VALUES` 的声明顺序相反）⇒ 两个计数取不同值。
    expect(summaryText(text)).toContain('已完成15笔，已暂停17笔，问题单16笔，已退单18笔')
  })

  it('后端漏给某个状态 ⇒ 它按 0 笔渲染（不出现 undefined）', () => {
    // 防御：契约说 8 个全给，但缺键时宁可显示 0 也不能把 `undefined` 或空串写进用户可见文案。
    // 本夹具缺 `PAUSED`（且 REFUNDED 归 0）⇒ 期望句不能复用 `SUMMARY_TEXT`（那里已退单是 2 笔），
    // 用本地字面钉整句（E-0024：断言钉值）。
    const text = renderSummaryText({
      ...SUMMARY,
      status_counts: {
        UNASSIGNED: 3,
        WAITING_GRAB: 0,
        IN_PROGRESS: 4,
        PENDING_REVIEW: 1,
        COMPLETED: 4,
        ISSUE: 0,
        REFUNDED: 0,
      },
    })
    expect(summaryText(text)).toBe(
      '本次搜索统计共12笔，预计总发单价3600.00元(不含接单提成)，总接单价4800.00元，' +
        '总退款180.00元，预计利润1200.00元(含已退单部分150.00元、含已退款部分180.00元)。其中：未分配3笔，待抢单0笔，代练中4笔，待验收1笔，' +
        '已完成4笔，已暂停0笔，问题单0笔，已退单0笔',
    )
    expect(text).not.toContain('undefined')
  })

  it('金额按后端给的字符串原样落句（不自己补 `.00`、不自己算 diff；负数照写）', () => {
    const text = renderSummaryText({
      ...SUMMARY,
      total_hall_price: '0.00',
      total_price: '7.50',
      diff: '-1.00',
    })
    expect(text).toContain('预计总发单价0.00元(不含接单提成)')
    expect(text).toContain('总接单价7.50元')
    expect(text).toContain('预计利润-1.00元')
  })

  it('后端万一回**数字**金额 ⇒ 先 String 再转义，不抛 `TypeError`', () => {
    // ⭐ `escapeHtml` 里是 `value.replace(...)` ⇒ 直接拿数字进去会
    // `TypeError: value.replace is not a function`（在 `done` 里炸 ⇒ 整页那句统计值不更新）。
    // 契约说金额是两位小数字符串，但笔数那一处本来就先 `String()` ⇒ 两处写法必须一致。
    const text = renderSummaryText({
      ...SUMMARY,
      total_hall_price: 3 as unknown as string,
      total_price: 7.5 as unknown as string,
      diff: -1 as unknown as string,
    })
    expect(text).toContain('预计总发单价3元(不含接单提成)')
    expect(text).toContain('总接单价7.5元')
    expect(text).toContain('预计利润-1元')
  })

  it('8 个中文 label 都真的在证据件 `select[name=orderSta]` 的 option 里（点它才会被写进筛选）', () => {
    // 点击靠 formSelects 在 `<dl>` 里找 `dd[lay-value="<中文>"]`；找不到时它的 `valToName` 回空串
    // ⇒ **静默什么都不写** ⇒ 表单里 `orderSta` 是空串 ⇒ 既不送 `status`、也不弹提示
    // （用户看到的是「筛选被悄悄清掉、返回全量」）。所以 8 个中文 label 都要逐一钉住。
    const select = /<select name="orderSta"[\s\S]*?<\/select>/.exec(EVIDENCE)?.[0] ?? ''
    expect(select, '证据件里找不到 select[name=orderSta]').not.toBe('')
    const optionValues = new Set(
      Array.from(select.matchAll(/<option[^>]*value="([^"]*)"/g)).map((m) => m[1]),
    )
    for (const status of ORDER_STATUS_VALUES) {
      expect(
        optionValues.has(ORDER_STATUS_LABELS[status]),
        `证据件 orderSta 的 option 里没有「${ORDER_STATUS_LABELS[status]}」（点了会静默无效）`,
      ).toBe(true)
    }
  })
})

/**
 * 本 describe 里「行为」用例默认给的身份：**七道门**各自需要的节点都给齐
 * （⛔ 不是「全给」——节点集见 `setOrderListPermissions`）。
 *
 * ⚠️ **为什么抽成常量 + 两个桩助手**：本组不少用例用
 * `mockedRequest.mockResolvedValue(载荷)` / `mockRejectedValue(err)` 顶掉**整个** `request`
 * 替身（它们只关心那一发业务请求），于是 mount 里的 `fetchMe()` 也拿到同一个东西 ⇒ 七道门
 * 全 false ⇒ 入口**不渲染**（工具栏 `setRecMan` 更是被摘掉）——那会伪装成「点了没反应」的行为回归。
 * `mockRequests` / `mockRequestsRejecting` 把「身份那一发」与「业务那一发」分开。
 *
 * ⚠️ 工具栏「导出」也按节点渲染（单点 `order.export`）⇒ 本表补该节点
 * （⛔ 不是「给全部节点」）。
 */
const ME_FOR_TESTS = {
  is_tenant_admin: false,
  is_account_admin: false,
  menu_permissions: [
    // ⭐ 平台写入口（发单地球 / 撤回箭头）挂 `order.platform_write`；后端节点未上线时
    // 前端按 `order.list` 降级（见 `@/layui-island/perms::canPlatformWrite`）⇒ 本夹具给这个节点。
    'order.list',
    'order.add_edit',
    'order.assign_taker',
    // 编辑订单的门（弹层第一发 `GET /orders/<no>/`）——⛔ 不是 add_edit。
    'order.view_detail',
    'order.view_screenshot',
    'order.upload_screenshot',
    // 发大厅 / 撤回两颗写入口的门。
    'order.set_offer_price',
    'order.withdraw_from_grab_hall',
    // 工具栏「导出」的门（后端 `OrderExportView` 只认这一个节点）。
    'order.export',
  ],
}

describe('指派打手 / 清除接单人（挂载整页 + 真实点击 + 真 api 层）', () => {
  const mockedRequest = vi.mocked(request)

  beforeEach(() => {
    LAYUI.open.mockReset()
    // ⚠️ `mockReset()` 会**抹掉实现** ⇒ 这里重装真 layui 形状的 `layer.open`（搬节点，B-Minor-1）。
    //    用例若自己 `mockImplementation(() => 7)`（要一个固定层索引）会覆盖它、于是不搬节点——
    //    那是**有意**的：那些用例不需要弹层 DOM，只判入参与层索引。
    LAYUI.__resetLayers()
    LAYUI.open.mockImplementation(LAYUI.realOpen)
    LAYUI.msg.mockClear()
    LAYUI.confirm.mockReset()
    LAYUI.confirm.mockImplementation((_text: string, options: unknown, yes?: (index: number) => void) => {
      if ((options as { title?: string } | undefined)?.title === '批量操作') yes?.(90)
      return 90
    })
    LAYUI.layer.close.mockClear()
    // ⭐ T1：`runBatchFlow` 现在会调 `layer.load` ⇒ 跨用例共享的替身，必须清。
    LAYUI.layer.load.mockClear()
    // T8 起「收款状态」弹窗的未选分支弹 `layer.alert`（与 `msg` 一样跨用例累积，须清）。
    LAYUI.layer.alert.mockClear()
    // ⚠️ `layer.prompt` 的调用会跨用例累积，「点订单截图不出 prompt」这类
    // `not.toHaveBeenCalled()` 判据会被上一支用例的记录污染 ⇒ 每个用例前清掉。
    LAYUI.layer.prompt.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.table.getOptions.mockReturnValue({ page: { limit: 20 } })
    LAYUI.table.checkStatus.mockReturnValue({ data: [] })
    LAYUI.form.render.mockClear()
    LAYUI.form.val.mockReturnValue({})
    vi.mocked(fetchAssigneeOptions).mockReset()
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    mockedRequest.mockReset()
    // ⭐ `requestEnvelope` 默认**转接**到 `request` 替身（`msg` 空串），
    // 这样既有「只配 request」的用例照旧能驱动指派全流程；要测警告的用例自己盖掉它。
    vi.mocked(requestEnvelope).mockReset()
    vi.mocked(requestEnvelope).mockImplementation(
      (async (config: unknown) => ({
        code: 0,
        msg: '',
        data: await mockedRequest(config as never),
      })) as never,
    )
    // ⚠️ **权限门**：页 mount 时会取一次 `/api/auth/me` 决定
    //    行内「指派打手」/「订单截图」/「编辑订单」/发大厅/撤回渲不渲染。本组要测那些入口的
    //    **行为** ⇒ 默认给齐七道门需要的节点（⛔ 不是「给全部节点」——七道门各自的节点集见
    //    `setOrderListPermissions`）；「没有节点 ⇒ 不渲染」那一支由本文件「权限门」一组
    //    **显式**驱动（它们自己改 `me`）。
    mockedRequest.mockImplementation((async (config: { url?: string }) => {
      if (config.url === '/api/auth/me') {
        return { user: ME_FOR_TESTS }
      }
      return {}
    }) as never)
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
    // `vi.stubEnv('BASE_URL', …)`（T5 基址守卫）的兜底还原：用例自己 `finally` 里也还原一次，
    // 这里再兜一道——用例中途失败时别把 `/sub/` 泄给后面的用例。
    vi.unstubAllEnvs()
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<{
    root: HTMLElement
    cleanup: () => void
  }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    // ⛔ T5 起 `mountOrderListPage` **不再收** `{ onNavigate }`：本页两个「进订单详情」入口
    //    都改成原地开层了（见 `order-orderlist.ts` 的「已知差异 21」），没有跳路由的入口
    //    ⇒ 那个注入口连类型一起删掉了（不是留个空占位）。
    const cleanup = await mountOrderListPage(root)
    renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], LAYUI.table.checkStatus().data)
      .forEach((box) => { box.checked = true })
    LAYUI.table.reload.mockClear()
    // ⚠️ **抹掉 mount 期的那一发 `request()`**（页 mount 会取一次
    //    `/api/auth/me`）。本文件多处判「一次**写**请求都不发」（`writes()` 按 `method !== 'get'`
    //    过滤，而 me 那一发是大写 `GET`）⇒ 不清会把权限那一发算成写请求。
    mockedRequest.mockClear()
    return { root, cleanup }
  }

  /**
   * 「身份那一发照 6 道门给全，其余 URL 回 `payload`」——给那些用 `mockResolvedValue(载荷)`
   * 顶掉整个替身的**行为**用例（见 `ME_FOR_TESTS` 的注释）。⛔ 权限门本身不靠它。
   */
  function mockRequests(payload: unknown): void {
    mockedRequest.mockImplementation((async (config: { url?: string }) =>
      config?.url === '/api/auth/me' ? { user: ME_FOR_TESTS } : payload) as never)
  }

  /** 同 `mockRequests`，但其余 URL **抛** `err`（给「那一发失败」的用例）。 */
  function mockRequestsRejecting(err: unknown): void {
    mockedRequest.mockImplementation((async (config: { url?: string }) => {
      if (config?.url === '/api/auth/me') return { user: ME_FOR_TESTS }
      throw err
    }) as never)
  }

  function clickToolbar(root: HTMLElement): void {
    const btn = root.querySelector<HTMLElement>('button[data-tb="setRecMan"]')
    expect(btn, '证据件里找不到 [data-tb=setRecMan]').not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /**
   * 经**行内**「接手人」那格（`a.jsr`）打开指派弹层 —— **单笔语义**入口（`{kind:'single'}`）。
   *
   * ⚠️ **工具栏那颗 `setRecMan` 走批量**
   * （`{kind:'batch'}`：`currentTaker` / `orderProject` / `orderHallPrice` **一律传空**，spec §5.2）
   * ⇒ 「从本单预勾选 / 预填三格 / 单笔 `/assign/` 载荷 / 校验文案里的『默认为 88.50』/ 清除接单人」
   * 这类**只对单笔成立**的判据，必须改从**行内**入口驱动（行内入口一字未改，spec §D11）。
   * 工具栏那一支的批量判据钉在 `order-orderlist-batch.spec.ts`。
   *
   * ⚠️ 节点必须挂进 `root`：行内点击靠 `root` 上的委托（`onCellClick`）。
   */
  function openViaInline(
    root: HTMLElement,
    row: Record<string, unknown> = { ...ROW, taker_name: 'taker1' },
  ): void {
    const cols = buildOrderCols(
      () => '原神',
      () => '',
      { isGroupGame: () => false },
    )
    const col = cols.find((c) => c.title === '接手人')
    expect(col, '列定义里找不到「接手人」').toBeDefined()
    const host = document.createElement('div')
    host.innerHTML = (col!.templet as unknown as (d: Record<string, unknown>) => string)(row)
    root.appendChild(host)
    const link = host.querySelector<HTMLElement>('a.jsr')
    expect(link, '行内「接手人」格没产出 a.jsr').not.toBeNull()
    link!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  /**
   * 工具栏那颗 `setRecMan`（「指派打手」）的**渲染门**（评审必修 2 的附带项）。
   *
   * 它与行内 `.jsr` 是同一个门（都打开同一个指派弹层 ⇒ 都会打 `assignee-options/` 名单端点），
   * 而它住在**证据件的静态 DOM**里 ⇒ 由 `applyOrderListPermissionGates(root)` 在
   * `root.innerHTML = 证据件` 之后**摘掉**。本用例走**真 mount**（⛔ 不是直接调那个函数）：
   * 判据是「mount 读了 `/api/auth/me` 之后工具栏里还剩什么」。
   */
  it('工具栏「指派打手」也按节点摘掉：六个节点全无 ⇒ 按钮不在 DOM 里；有任一 assign 节点 ⇒ 在', async () => {
    const withMe = (nodes: string[]): void => {
      mockedRequest.mockImplementation((async (config: { url?: string }) =>
        config?.url === '/api/auth/me'
          ? { user: { is_tenant_admin: false, is_account_admin: false, menu_permissions: nodes } }
          : {}) as never)
    }
    withMe([])
    {
      const { root, cleanup } = await mountPage()
      expect(
        root.querySelector('button[data-tb="setRecMan"]'),
        '没有指派节点时工具栏那颗仍渲染 ⇒ 勾一笔点它会打 assignee-options/ 名单端点（403）',
      ).toBeNull()
      cleanup()
    }
    withMe(['order.submit_payout_price'])
    {
      const { root, cleanup } = await mountPage()
      expect(
        root.querySelector('button[data-tb="setRecMan"]'),
        '只给 order.submit_payout_price 时工具栏那颗被摘掉了（过度隐藏，评审必修 2）',
      ).not.toBeNull()
      cleanup()
    }
  })

  /** 排空已 resolve 的 promise 链（一个宏任务够用，同「改状态」弹窗的用例）。 */
  async function flush(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }

  function lastOpenOpts(): Record<string, unknown> {
    const calls = LAYUI.open.mock.calls
    return calls[calls.length - 1]![0] as Record<string, unknown>
  }

  /**
   * 某个 `vi.fn()` 的**最后一次**调用的参数元组。
   *
   * ⚠️ 不用 `Array.prototype.at`：本工程的 tsconfig `lib` 早于 ES2022，`vue-tsc` 会报 TS2550。
   */
  function lastCall<A extends unknown[]>(fn: { mock: { calls: A[] } }): A {
    return fn.mock.calls[fn.mock.calls.length - 1]!
  }

  /**
   * 「收款状态」弹层节点（`.setDiv`）：真 layui 已把它搬进 `.layui-layer-content`（`root` 之外）
   * ⇒ 必须**文档级**取（与实现里 `settPanel()` 同一判据；B-Minor-1）。
   */
  function settDialog(): HTMLElement {
    return document.querySelector<HTMLElement>('.layui-layer-content > .setDiv')!
  }

  /**
   * 最近一次 `table.render` 的配置。Task 8 的统计句要从这里拿 `parseData` / `done` ——
   * 那是 layui 取数时的**真实调用链**（读码见下面 `fetchFrame` 的注释）。
   */
  function lastRenderOpts(): {
    parseData?: (r: Record<string, unknown>) => unknown
    done?: (r: unknown, c?: number, n?: number) => void
  } {
    const calls = LAYUI.table.render.mock.calls
    return (calls[calls.length - 1]?.[0] ?? {}) as {
      parseData?: (r: Record<string, unknown>) => unknown
      done?: (r: unknown, c?: number, n?: number) => void
    }
  }

  /**
   * 用页面交出的 `content` 造出弹层节点，再调它自己的 `success(layero, index)`——
   * 这正是真 layui 插好内容后走的回调（替身不代劳插 DOM）。
   *
   * ⚠️ **T6 起这里把节点挂进 `.layui-layer-content`**（照真 layui 那一手）：指派弹层的内容是
   * 「左列多选列表 + 右列两张表」的真实 DOM，判据（spec §8 第 21/28 条：「右表恰好一行」、
   * 「列标题恰好是…」）必须打在**文档里的真实节点**上；`mockImplementation(() => 7)` 的替身
   * 不搬节点、只记录入参 ⇒ 不挂的话实现的 `panel()`（文档级优先）与用例的
   * `.user` / `table.tt-assign-takers` 都取不到。三层都在 `document.body` 上，
   * `afterEach` 的 `document.body.innerHTML = ''` 负责清场。
   */
  function runDialogSuccess(): HTMLElement {
    const opts = lastOpenOpts()
    const success = opts.success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.id = 'tt-order-assign'
    dialog.innerHTML = String(opts.content)
    const layer = document.createElement('div')
    layer.className = 'layui-layer'
    const content = document.createElement('div')
    content.className = 'layui-layer-content'
    layer.appendChild(content)
    content.appendChild(dialog)
    document.body.appendChild(layer)
    success({ find: () => ({ 0: dialog }) }, 7)
    return dialog
  }

  /* ═══════════ Task 6：单人版指派弹层（spec §7.1 的九个区块） ═══════════ */

  /** 弹层内容里那个「打手多选列表」容器（`.user`，照对标 `raw/assignation.html:165`）。 */
  function userListBox(): HTMLElement {
    const box =
      document.querySelector<HTMLElement>('.layui-layer-content .user') ??
      document.querySelector<HTMLElement>('.user')
    expect(box, '找不到打手多选列表容器 .user').not.toBeNull()
    return box!
  }

  /** 弹层内容里那张「当前订单分配接手人」表（手工标记）。 */
  function takerTable(): HTMLElement {
    const table = document.querySelector<HTMLElement>('table.tt-assign-takers')
    expect(table, '找不到当前接手人表').not.toBeNull()
    return table!
  }

  function takerRowEls(): HTMLElement[] {
    return [...takerTable().querySelectorAll<HTMLElement>('tbody tr')]
  }

  /** 勾选 / 取消第 index 个打手（checkbox 的 `name="userList"`、`value` = 账号 id）。 */
  function checkUser(index: number, checked = true): HTMLInputElement {
    const boxes = userListBox().querySelectorAll<HTMLInputElement>('input[name="userList"]')
    const box = boxes[index]!
    box.checked = checked
    box.dispatchEvent(new Event('change', { bubbles: true }))
    return box
  }

  /**
   * B11：点右表某一格的 `<a>` ⇒ 格内出现 `<input>`（**只改内存、不发请求、不弹窗**）。
   * 返回那个输入框，交给调用方填值 / 派发 `blur` / `Enter` / `Escape`。
   */
  function beginInlineEdit(field: string): HTMLInputElement {
    const cell = takerRowEls()[0]!.querySelector<HTMLElement>(`[data-cell="${field}"]`)
    expect(cell, `右表里找不到 ${field} 格`).not.toBeNull()
    cell!.querySelector<HTMLElement>('a')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const input = cell!.querySelector<HTMLInputElement>('input')
    expect(input, `点了 ${field} 格之后格内没有 <input>`).not.toBeNull()
    return input!
  }

  /** 行内编辑一格并提交（默认 `blur`；U6 的另一档用 `enter`）。 */
  function editCellInline(field: string, value: string, how: 'blur' | 'enter' = 'blur'): void {
    const input = beginInlineEdit(field)
    input.value = value
    if (how === 'enter') {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    } else {
      input.dispatchEvent(new Event('blur'))
    }
  }

  /**
   * 模拟「正在编辑 A 格时，鼠标直接点到 B 格」这一串真实事件，返回 B 格里新出现的 `<input>`
   * （没进编辑态则 `null`）。
   *
   * ⚠️ 判据的关键在**派发顺序与目标**：真实浏览器里 `mousedown` 落在 B 格 ⇒ A 格失焦
   *    （`blur` → 提交）⇒ 之后才轮到 `mouseup` / `click`。而 `click` 只在 `mousedown` 的
   *    目标还在文档里时才回得到祖先；若提交把整段 `tbody.innerHTML` 重建，B 格那颗 `<a>`
   *    已经被摘出文档 ⇒ 这一次 `click` 到不了弹层上的委托监听器（**第一次点击被吞**）。
   *
   * ⛔ **必须在 `blur` 之前先把 B 格那颗 `<a>` 抓在手里**（= 浏览器 `mousedown` 的目标）。
   *    若在 `blur` 之后重新查询，拿到的是重渲染后的**新** `<a>`——那样点谁都能进编辑态，
   *    判据对这个缺陷**恒绿**（同文件在册的 U6 就是这种「重查」写法，故它抓不到本缺陷）。
   */
  function clickThroughTo(input: HTMLInputElement, nextField: string): HTMLInputElement | null {
    const nextCell = takerRowEls()[0]!.querySelector<HTMLElement>(`[data-cell="${nextField}"]`)
    expect(nextCell, `右表里找不到 ${nextField} 格`).not.toBeNull()
    const anchorBeforeCommit = nextCell!.querySelector<HTMLElement>('a')
    expect(anchorBeforeCommit, `${nextField} 格里没有 <a>`).not.toBeNull()
    input.dispatchEvent(new Event('blur'))
    anchorBeforeCommit!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const liveCell = takerRowEls()[0]!.querySelector<HTMLElement>(`[data-cell="${nextField}"]`)!
    return liveCell.querySelector<HTMLInputElement>('input')
  }

  it('弹层壳：标题 / 尺寸照对标（85%×95%）、type:1，且**不是** iframe', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    const opts = lastOpenOpts()
    expect(opts.type).toBe(1)
    expect(opts.title).toBe('将订单指派给接手人/代练员')
    expect(opts.area).toEqual(['85%', '95%'])
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(5)
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.shadeClose).toBe(true)
    expect(typeof opts.content).toBe('string') // 页内自建弹层（不是 iframe 的 URL 串）
    cleanup()
  })

  it('九个区块的标记：职位「全部」+接口返回的 roles（默认代练员）、业务类型三档、搜索框 maxlength、两张表的表头、两颗底部按钮', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    const dialog = runDialogSuccess()
    // ⚠️ B12b 起「用户职位」那一组的值来自**接口响应**（`AssigneeOptions.roles`）⇒ 必须等
    //    那条 promise 落地（硬编码内置角色名是**旧值**）。
    await flush()

    // 区块 1：用户职位（「全部」= 不筛 + 接口回的 roles；默认选中「代练员」，C16 / §9 V5）
    const roles = [...dialog.querySelectorAll<HTMLInputElement>('input[name="userRole"]')]
    expect(roles.map((r) => r.value)).toEqual(['', ...ACCOUNTS.roles])
    expect(roles.map((r) => r.getAttribute('title'))).toEqual(['全部', ...ACCOUNTS.roles])
    expect(roles.filter((r) => r.checked).map((r) => r.value)).toEqual(['代练员'])
    // 区块 2：业务类型三档，title 逐字（默认 0 档）
    const buss = [...dialog.querySelectorAll<HTMLInputElement>('input[name="bussType"]')]
    expect(buss.map((b) => b.value)).toEqual(['0', '1', '2'])
    expect(buss.map((b) => b.getAttribute('title'))).toEqual(['代练+陪玩', '代练', '陪玩'])
    expect(buss.filter((b) => b.checked).map((b) => b.value)).toEqual(['0'])
    // 区块 4：搜索打手
    const key = dialog.querySelector<HTMLInputElement>('input#key1')!
    expect(key.getAttribute('placeholder')).toBe('用户名/名称/标记')
    expect(key.getAttribute('maxlength')).toBe('64') // 我们的上界（V6）
    const sech = dialog.querySelector<HTMLElement>('button.sechbtn')!
    expect(sech.textContent).toContain('搜索')
    // 区块 5：多选列表容器
    expect(dialog.querySelector('.user')).not.toBeNull()
    // 区块 6：右列表头逐字（顺序也断，取自 `recon-live` §2.3）
    expect([...takerTable().querySelectorAll('thead th')].map((th) => th.textContent)).toEqual([
      '当前接手人',
      '发单价(点击编辑)',
      '时间要求(小时)',
      '接手项目(点击可单独设定)',
      '操作',
    ])
    // 区块 7：账单表的 legend 与表头逐字（四列 + 操作）
    expect(dialog.textContent).toContain('该订单的打手账单')
    expect(dialog.textContent).toContain(
      '【↓中途换打手请从下面列表中把没用的账单删除，这关系到后面与代练结算↓】',
    )
    const bill = document.querySelector<HTMLElement>('table.tt-assign-bills')!
    expect([...bill.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual([
      '用户名',
      '预计绩效',
      '生效绩效',
      '记录时间',
      '操作',
    ])
    // 区块 8：底部两颗按钮的逐字文案（对标两颗恒在 ⇒ 没有接单人也照渲染）
    expect(dialog.textContent).toContain('保存指派(没改动请勿点)')
    expect(dialog.textContent).toContain('清除当前所有接手人')
    // 区块 9（spec §7.1 第 9 行 / §9 V9）：对标那 11 个 hidden **一个都不渲染**。
    // 两条判据缺一不可：① 计数为 0（钉「没有 hidden」）；② 逐个 name 不存在
    // （钉「不是换个 type 又把同一批字段塞回来」）。name 清单逐字取 spec §7.1。
    expect(dialog.querySelectorAll('input[type="hidden"]').length).toBe(0)
    for (const hiddenName of [
      'defaultJobId',
      'taskId',
      'gameName',
      'orderId',
      'orderProject',
      'orderHours',
      'repeatedlyAss',
      'price',
      'isAutoSwitch',
      'firstOrderBussType',
      'taskNeedJobId',
    ]) {
      expect(dialog.querySelector(`input[name="${hiddenName}"]`)).toBeNull()
    }
    cleanup()
  })

  it('五处「对标有、我方不做」的标记一个都渲染不出来（C2/C3/C4/C5 的逐条钉子）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    const dialog = runDialogSuccess()
    const html = dialog.innerHTML
    expect(dialog.querySelector('input[name="autoAss"]')).toBeNull() // 智能派单
    expect(dialog.querySelector('input[name="sphone"]')).toBeNull() // 指定上号电话
    expect(dialog.querySelector('input[name="orderPricePerc"]')).toBeNull() // 按价格百分比
    expect(dialog.querySelector('input[name="userOrderBy"]')).toBeNull() // 排序方式
    expect(dialog.querySelector('.saveConfigBtn')).toBeNull() // 保存筛选
    expect(html).not.toContain('开启智能派单')
    expect(html).not.toContain('指定上号电话')
    expect(html).not.toContain('保存筛选')
    cleanup()
  })

  it('弹层打开时对两组 radio 手动 form.render(\'radio\')（不渲染 ⇒ 用户职位 / 业务类型只剩没有文字的圆圈）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    // 清掉「挂载 / 开层之前」的记录 ⇒ 下面的判据只钉**这一层弹层的 `success`** 自己发出的调用。
    LAYUI.form.render.mockClear()
    const dialog = runDialogSuccess()
    // 前置：弹层里确实有运行期插入的 radio（否则下面的判据可能是空集上的恒真）。
    expect(dialog.querySelectorAll('input[type="radio"]').length).toBeGreaterThan(0)
    // 手动 render 的依据（读 vendored `public/layui/layui.js` 的 form 模块）：`render` 取的容器是
    // `.layui-form`、radio 取容器内 `input[type=radio]`，而 layui 只在页面初始化 / reset / submit 时
    // 自动渲染 ⇒ 运行期插进 layer 的这一层必须自己 render，否则 `title` 不会变成可见文案。
    expect(LAYUI.form.render).toHaveBeenCalledWith('radio')
    // ⛔ 既有行为不许改弱：候选名单那批 checkbox 仍要 render（`renderUserList()` 内）。
    expect(LAYUI.form.render).toHaveBeenCalledWith('checkbox')
    cleanup()
  })

  it('名单请求：默认 role=代练员；切「全部」不送 role；选「代练」送 business_type=1；搜索送 key', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    await flush()
    const dialog = runDialogSuccess()
    expect(fetchAssigneeOptions).toHaveBeenLastCalledWith(
      expect.objectContaining({ role: '代练员', limit: 200 }),
    )

    // 切「全部」⇒ 不带 role 这个键（不是 role:''）
    const all = dialog.querySelector<HTMLInputElement>('input[name="userRole"][value=""]')!
    all.checked = true
    all.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    const allCall = lastCall(vi.mocked(fetchAssigneeOptions))[0] as Record<string, unknown>
    expect('role' in allCall).toBe(false)

    const buss = dialog.querySelector<HTMLInputElement>('input[name="bussType"][value="1"]')!
    buss.checked = true
    buss.dispatchEvent(new Event('change', { bubbles: true }))
    await flush()
    expect(fetchAssigneeOptions).toHaveBeenLastCalledWith(expect.objectContaining({ business_type: 1 }))

    const key = dialog.querySelector<HTMLInputElement>('input#key1')!
    key.value = '三角洲'
    dialog
      .querySelector<HTMLElement>('button.sechbtn')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(fetchAssigneeOptions).toHaveBeenLastCalledWith(expect.objectContaining({ key: '三角洲' }))
    cleanup()
  })

  it('名单按显示名升序（乱序响应 ⇒ DOM 顺序是升序）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    vi.mocked(fetchAssigneeOptions).mockResolvedValue({
      items: [
        { id: 3, name: '丙' },
        { id: 1, name: '甲' },
        { id: 2, name: 'b' },
      ],
      truncated: false,
    } as never)
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    await flush()
    runDialogSuccess()
    const labels = [...userListBox().querySelectorAll<HTMLInputElement>('input[name="userList"]')].map(
      (b) => b.getAttribute('title'),
    )
    expect(labels).toEqual(['b', '丙', '甲']) // 后端已把空昵称回落成 'b'，按确定性码点升序
    cleanup()
  })

  it('勾选一人 ⇒ 右表恰好一行且那一格 = 被勾的人的显示名；勾第二人 ⇒ 行不变 + 提示', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    await flush()
    runDialogSuccess()
    await flush()

    checkUser(0)
    expect(takerRowEls()).toHaveLength(1)
    expect(takerRowEls()[0]!.textContent).toContain('打手一')

    // 勾第二个 ⇒ 右表**不变**、提示出现（用的是**已勾选那个人**的名字，spec §7.1 的原文口径）、
    // 第二个 checkbox **未被选中**
    const second = checkUser(1)
    expect(takerRowEls()).toHaveLength(1)
    expect(takerRowEls()[0]!.textContent).toContain('打手一')
    expect(second.checked).toBe(false)
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '打手一已是本单接手人，一个订单只能有一个接手人，请先取消',
      { time: 1500 },
    )
    cleanup()
  })

  it('打开时用读接口回来的那行预勾选并把三格预填（spec §7.1 的「预勾选 / 预填」；发单价来自订单行）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    // ⭐ B12：发单价那一格显示的是**订单发单价**（列表同源的 `hall_price`），**不是**绩效行的
    //    `temp_amount`。夹具刻意让两者不同（88.50 ≠ 66.00）⇒ 「读错了源」这一支必红。
    // ⚠️ 工具栏走批量（三个初值一律空）⇒ 这条**单笔预填**判据改从行内 `.jsr` 入口驱动。
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    // 读接口（`fetchPerformances({order_no, emp_type:'打单工资'})`）回来的那一行
    mockRequests({
      items: [
        {
          id: 31,
          order_no: ROW.order_no,
          user: 7,
          display_name: '打手一',
          temp_amount: '66.00',
          bonus_amount: '0.00',
          hours: 24,
          taker_project: '三角洲',
          // D-0302：后端该列已是 +08:00 的 ISO 串 ⇒ 「记录时间」格截
          // `YYYY-MM-DD HH:mm` 直显（不整串进单元格）。
          created_at: '2026-10-24T12:30:00+08:00',
        },
      ],
    } as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW, taker_name: '打手一', hall_price: '88.50' })
    await flush()
    runDialogSuccess()
    await flush()

    // 预勾选：名单里 id=7 那个 checkbox 是选中的
    const checked = userListBox().querySelectorAll<HTMLInputElement>('input[name="userList"]:checked')
    expect([...checked].map((b) => b.value)).toEqual(['7'])
    // 预填：右表那一行的三格 —— 发单价取订单行 `hall_price`（88.50），
    // 时间要求 / 接手项目仍取绩效行（24 / 三角洲）
    const cells = takerRowEls()[0]!.querySelectorAll<HTMLElement>('[data-cell]')
    expect([...cells].map((c) => c.textContent)).toEqual(['88.50', '24', '三角洲'])
    // 「记录时间」格（D-0302）：绩效行的 `created_at` 截成 `YYYY-MM-DD HH:mm` 直显。
    const billCells = document.querySelectorAll<HTMLElement>(
      'table.tt-assign-bills tbody tr:first-child td',
    )
    expect(billCells[3]!.textContent).toBe('2026-10-24 12:30')
    cleanup()
  })

  it('取消勾选 ⇒ 右表那一行消失', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    await flush()
    runDialogSuccess()
    const box = checkUser(0)
    expect(takerRowEls()).toHaveLength(1)
    box.checked = false
    box.dispatchEvent(new Event('change', { bubbles: true }))
    expect(takerRowEls()).toHaveLength(0)
    cleanup()
  })

  it('保存：行内改过三格时 body 恰好 `{user_id, price, hours, project}`（送 price、⛔ 不送 fee）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    // 工具栏走批量 ⇒ 单笔 `/assign/` 载荷判据从行内 `.jsr` 入口驱动。
    openViaInline(root, { ...ROW, hall_price: '100.00' })
    await flush()
    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)
    // 行内编辑三格（B11：点一下出 `<input>`、`blur` 提交；⛔ 不发任何请求）
    editCellInline('price', '88.50')
    editCellInline('hours', '24')
    editCellInline('project', '三角洲')

    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
    const config = lastRequestConfig()
    expect(config).toEqual({
      method: 'post',
      url: `/api/business/orders/${ROW.order_no}/assign/`,
      data: { user_id: 7, price: '88.50', hours: 24, project: '三角洲' },
    })
    // 「不再送 fee」在**载荷**上钉死（U5）：`fee` 写的是 `Performance.temp_amount`，已换键。
    expect('fee' in (config.data as Record<string, unknown>)).toBe(false)
    cleanup()
  })

  it('保存：订单**没有**发单价（`hall_price` 为 null）⇒ body 里没有 price / fee / hours 这三个键', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    // 工具栏走批量 ⇒ 这条「初值取本单」的判据从行内 `.jsr` 入口驱动。
    openViaInline(root, { ...ROW })
    await flush()
    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)

    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
    const data = lastRequestConfig().data as Record<string, unknown>
    // `project` 的初值 = 该订单的 `project_text`（对标 hidden `orderProject` 同形）⇒ 这一档会送它；
    // 那一格空着（订单本来就没有发单价）⇒ 不送空串（后端会当非法金额拒）。
    expect(data).toEqual({ user_id: 7, project: '原神-深渊' })
    expect('price' in data).toBe(false)
    expect('fee' in data).toBe(false)
    expect('hours' in data).toBe(false)
    cleanup()
  })

  it('保存：订单**有**发单价而用户没动那一格 ⇒ 照送 `price`（与对标 `postSave` 把整份数组送出去的形状一致）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW, hall_price: '88.50' })
    await flush()
    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)

    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
    const data = lastRequestConfig().data as Record<string, unknown>
    expect(data).toEqual({ user_id: 7, price: '88.50', project: '原神-深渊' })
    expect('fee' in data).toBe(false)
    cleanup()
  })

  it('没选人就保存 ⇒ 提示且一次**写**请求都不发', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    await flush()

    const dialog = runDialogSuccess()
    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(LAYUI.msg).toHaveBeenCalledWith('请选择代练员', { time: 1500 })
    // ⚠️ 判据按**写请求**收窄（T6 的弹层自带一条 `GET /performances/` 取账单，那是正常取数）：
    // 「没选人 ⇒ 一个写动作都不发」这条没有放宽，只是把读请求排除在判据之外。
    const writes = mockedRequest.mock.calls.filter(
      (c) => (c[0] as { method?: string }).method !== 'get',
    )
    expect(writes).toHaveLength(0)
    cleanup()
  })

  it('校验文案逐字：发单价非金额 / hours 填 0 / project 清空 ⇒ 各弹对标原文、失败后格子还原', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    // 那句「默认为**66.00**」的原值来自**本单的发单价**（单笔预填）⇒ 从行内 `.jsr` 入口驱动。
    openViaInline(root, { ...ROW, hall_price: '66.00' })
    await flush()
    runDialogSuccess()
    await flush()
    checkUser(0)

    const editCell = (cell: string, value: string): void => {
      const input = beginInlineEdit(cell)
      input.value = value
      input.dispatchEvent(new Event('blur'))
    }
    // 发单价：形如金额但非法 ⇒ 逐字钉 `请输入正确的金额，默认为<原值>`
    editCell('price', 'abc')
    expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的金额，默认为66.00', { time: 1500 })
    expect(takerRowEls()[0]!.querySelector<HTMLElement>('[data-cell="price"]')!.textContent).toBe(
      '66.00',
    )
    editCell('hours', '0')
    expect(LAYUI.msg).toHaveBeenCalledWith('时间要求（小时）最小为1小时，请重输', { time: 1500 })
    editCell('project', '')
    expect(LAYUI.msg).toHaveBeenCalledWith('项目要求不能为空，请输入', { time: 1500 })
    cleanup()
  })

  it('清除：两步 confirm，第二句按钮文案逐字；第一颗按钮送 Y', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    // ⚠️ 「清除接单人」是**按单**动作、没有批量端点 ⇒ 这条判据只能从**行内**入口驱动
    // （批量档下弹层里的 `orderNo` 是空串，见报告里的疑虑）。
    openViaInline(root, { ...ROW, taker_name: 'taker1' })
    await flush()
    const dialog = runDialogSuccess()

    dialog
      .querySelector<HTMLElement>('[data-act="assign-clear"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const first = lastCall(LAYUI.confirm)
    expect(first[0]).toBe('提示：这个操作会清除该订单当前所有的接手人，是否继续？')
    ;(first[1] as (i: number) => void)(3)
    // ⭐ R9 #15（spec §7.1 第 5 条）：第一跳确认 ⇒ 关**第一跳那个 index**，且恰好一次。
    expect(LAYUI.layer.close).toHaveBeenCalledTimes(1)
    expect(LAYUI.layer.close).toHaveBeenCalledWith(3)
    const second = lastCall(LAYUI.confirm)
    expect(second[0]).toBe('是否需要同步删除代练接手人的绩效记录？')
    expect((second[1] as unknown as { btn: string[] }).btn).toEqual([
      '需要删除接手人绩效',
      '不需要删除接手人绩效',
    ])
    ;(second[2] as (i: number) => void)(4)
    // ⭐ R9 #15：第二跳成功 ⇒ 关第二跳确认层（4）**与**指派弹层（7，`runDialogSuccess` 注入的层号）。
    // 关弹层发生在请求 `.then` 里 ⇒ 要 `waitFor`（`mockedRequest` 被调用 ≠ 那一发已回来）。
    await vi.waitFor(() => expect(LAYUI.layer.close).toHaveBeenCalledWith(7))
    expect(lastRequestConfig()).toEqual({
      method: 'post',
      url: `/api/business/orders/${ROW.order_no}/clear-taker/`,
      data: { isDelEmp: 'Y' },
    })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(4)
    cleanup()
  })

  it('清除：第二颗按钮（不需要删除接手人绩效）送 `isDelEmp: N`', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW, taker_name: 'taker1' })
    await flush()
    const dialog = runDialogSuccess()

    dialog
      .querySelector<HTMLElement>('[data-act="assign-clear"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const first = lastCall(LAYUI.confirm)
    ;(first[1] as (i: number) => void)(3)
    const second = lastCall(LAYUI.confirm)
    // 第二个按钮是 `layer.confirm(text, options, yes, btn2)` 的第 4 个参数
    ;(second[3] as (i: number) => void)(4)
    // ⭐ R9 #15：`Y` / `N` 两档走同一个 `doClearTaker` ⇒ 两档都要关「确认层 + 指派弹层」。
    await vi.waitFor(() => expect(LAYUI.layer.close).toHaveBeenCalledWith(7))
    expect(lastRequestConfig()).toEqual({
      method: 'post',
      url: `/api/business/orders/${ROW.order_no}/clear-taker/`,
      data: { isDelEmp: 'N' },
    })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(4)
    cleanup()
  })

  /**
   * ⭐ R9 #15 的失败支（spec §7.1 第 5 条末句「失败 ⇒ 不关弹层」）。
   *
   * ⚠️ 这里判的是**指派弹层**（`dialogIndex`）不关：第二跳确认层是 `doClearTaker` 的**先关后发**
   * 形态（它在发请求之前就关掉了确认层），所以失败时 `layer.close(4)` 已经发生过——如实登记。
   */
  it('清除：第二跳请求失败 ⇒ 弹后端 `msg` 原文；确认层已关、**指派弹层不关**（成功才关）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    mockedRequest.mockImplementation((async (config: { url?: string }) => {
      if (config.url === '/api/auth/me') return { user: ME_FOR_TESTS }
      if (String(config.url).includes('/clear-taker/')) {
        throw new BizError(1002, '清除接单人失败甲')
      }
      return {}
    }) as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW, taker_name: 'taker1' })
    await flush()
    const dialog = runDialogSuccess()

    dialog
      .querySelector<HTMLElement>('[data-act="assign-clear"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const first = lastCall(LAYUI.confirm)
    ;(first[1] as (i: number) => void)(3)
    const second = lastCall(LAYUI.confirm)
    ;(second[2] as (i: number) => void)(4)
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('清除接单人失败甲', { time: 1500 }),
    )
    expect(LAYUI.layer.close).toHaveBeenCalledWith(4)
    expect(LAYUI.layer.close).not.toHaveBeenCalledWith(7)
    cleanup()
  })

  it('「无权查看」的行：工具栏点了只提示，不开弹层、不拉名单', async () => {
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW, taker_name: '无权查看' }] })
    const { root, cleanup } = await mountPage()

    clickToolbar(root)

    expect(LAYUI.open).not.toHaveBeenCalled()
    // 文案逐字取 spec §9（「无权查看接手人」）——**不**用 `stringContaining` 放宽。
    expect(LAYUI.msg).toHaveBeenCalledWith('无权查看接手人', { time: 1500 })
    expect(fetchAssigneeOptions).not.toHaveBeenCalled()
    cleanup()
  })

  it('清单一拉失败也要能开弹层，并提示', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(fetchAssigneeOptions).mockRejectedValue(new Error('网络炸了'))
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    const { root, cleanup } = await mountPage()

    clickToolbar(root)
    await flush()

    expect(LAYUI.open).toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      expect.stringContaining('名单'),
      expect.anything(),
    )
    cleanup()
  })

  it('指派失败 ⇒ 只弹后端 `msg` 原文，不关弹层、不刷新（spec §5.1）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    mockRequestsRejecting(
      new BizError(1002, '只有未分配的单可以指派，当前状态：IN_PROGRESS'),
    )
    LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
    vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
    const { root, cleanup } = await mountPage()
    clickToolbar(root)
    await flush()

    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)
    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '只有未分配的单可以指派，当前状态：IN_PROGRESS',
        { time: 1500 },
      ),
    )
    expect(dialogCloses()).toEqual([])
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    cleanup()
  })

  it('指派成功：后端 msg 带「未能撤回」警告 ⇒ **原文**用 `layer.alert` 显示 + 刷新（⛔ 不吞）', async () => {
    const WARNING =
      '代练通订单未能撤回（原话：可操作资金不足，不能发布.），' +
      '请先到「第三方平台订单管理」撤回代练通平台订单，再指派打手'
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(requestEnvelope).mockResolvedValue({ code: 0, msg: WARNING, data: {} } as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW })
    await flush()
    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)
    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() => expect(LAYUI.layer.alert).toHaveBeenCalled())
    // 逐字原样（不是「指派成功」那种固定文案），且走的是**要人工确认**的 alert。
    expect(String(lastCall(LAYUI.layer.alert)[0])).toBe(WARNING)
    expect(LAYUI.msg).not.toHaveBeenCalledWith('指派成功', { time: 1500 })
    expect(LAYUI.table.reload).toHaveBeenCalled()
    cleanup()
  })

  it('指派成功：后端 msg 非空且不是警告 ⇒ 原样 `layer.msg`，停留更久（6s）', async () => {
    const OK_MSG = '指派成功，代练通订单已撤回'
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(requestEnvelope).mockResolvedValue({ code: 0, msg: OK_MSG, data: {} } as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW })
    await flush()
    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)
    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(OK_MSG, { time: 6000 }))
    expect(LAYUI.layer.alert).not.toHaveBeenCalled()
    expect(LAYUI.table.reload).toHaveBeenCalled()
    cleanup()
  })

  it('指派成功：后端 msg 为空 ⇒ 默认「指派成功」（不回归）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    vi.mocked(requestEnvelope).mockResolvedValue({ code: 0, msg: '', data: {} } as never)
    const { root, cleanup } = await mountPage()
    openViaInline(root, { ...ROW })
    await flush()
    const dialog = runDialogSuccess()
    await flush()
    checkUser(0)
    dialog
      .querySelector<HTMLElement>('[data-act="assign-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('指派成功', { time: 1500 }),
    )
    expect(LAYUI.layer.alert).not.toHaveBeenCalled()
    expect(LAYUI.table.reload).toHaveBeenCalled()
    cleanup()
  })

  it('行内「接手人」那格也是入口：点 `.jsr` 打开同一个弹层（带的是那一行的单号）', async () => {
    LAYUI.open.mockImplementation(() => 7)
    const { root, cleanup } = await mountPage()
    // 用页面**真实的** templet 造出那一格（不手抄标记，避免与实现分叉）。
    const cols = buildOrderCols(
      () => '原神',
      () => '',
      { isGroupGame: () => false },
    )
    const cell = document.createElement('div')
    cell.innerHTML = (cols.find((c) => c.title === '接手人')!.templet as (d: typeof ROW) => string)(
      ROW,
    )
    root.appendChild(cell)

    cell
      .querySelector<HTMLElement>('a.jsr')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(LAYUI.open).toHaveBeenCalled()
    // ⚠️ B12b 起第一趟名单请求**不带 `role`**（职位名要等这一趟的 `roles` 回来才知道，
    //    后端对不存在的职位名整条拒）⇒ 默认档那一趟筛子是**补**出来的，等它落地再断言。
    await flush()
    expect(fetchAssigneeOptions).toHaveBeenLastCalledWith(
      expect.objectContaining({ role: '代练员' }),
    )
    cleanup()
  })

  /* ═══════════ D-0147：§5-B 保留块 1:1 与发单价格语义（U5–U8） ═══════════ */

  /**
   * spec §6.2 的 U5–U8。
   *
   * 这一组覆盖：发单价那一格的**数据源与载荷键**（U5，spec §4.2）、三格改
   * **行内编辑**（U6，B11）、「用户职位」读**接口返回的 roles**（U7，B12b/R9）、
   * 以及 §5-B 表里那批**逐字值**（U8）。
   * ⛔ 不做整体复刻：`保存筛选` / `排序方式` / `开启智能派单` / `指定上号电话` / 11 个 hidden
   * 一个都不补（既有那两条用例仍在守）。
   */
  describe('§5-B 保留块 1:1 + 发单价格语义（U5–U8，D-0147）', () => {
    /** 写请求（`method !== 'get'`）——弹层自带的账单 GET 不算「动作」。 */
    function writes(): unknown[] {
      return mockedRequest.mock.calls.filter(
        (c) => (c[0] as { method?: string }).method !== 'get',
      )
    }

    it('U5：右表「发单价」显示**订单发单价**（不是绩效行 temp_amount）；保存送 price、⛔ 不送 fee', async () => {
      LAYUI.open.mockImplementation(() => 7)
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      // 绩效行的 `temp_amount` 刻意取一个**不同**的数（66.00）：显示成它这一支必红（能分辨数据源）
      mockRequests({
        items: [
          {
            id: 31,
            order_no: ROW.order_no,
            user: 7,
            display_name: '打手一',
            temp_amount: '66.00',
            bonus_amount: '0.00',
            hours: 24,
            taker_project: '三角洲',
          },
        ],
      } as never)
      const { root, cleanup } = await mountPage()
      // 工具栏走批量 ⇒ 这条依赖「本单发单价预填」的判据从行内 `.jsr` 入口驱动。
      openViaInline(root, { ...ROW, hall_price: '88.50' })
      await flush()
      const dialog = runDialogSuccess()
      await flush()
      checkUser(0)
      await flush()

      const priceCell = takerRowEls()[0]!.querySelector<HTMLElement>('[data-cell="price"]')!
      expect(priceCell.textContent).toBe('88.50')
      expect(priceCell.textContent).not.toBe('66.00')

      // 用户改过这一格后保存 ⇒ 载荷送 `price`（写 `Order.hall_price`），`fee` 整个不出现。
      editCellInline('price', '77.70')
      dialog
        .querySelector<HTMLElement>('[data-act="assign-save"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await vi.waitFor(() => expect(writes()).toHaveLength(1))
      const data = (lastRequestConfig().data ?? {}) as Record<string, unknown>
      expect(data.price).toBe('77.70')
      expect('fee' in data).toBe(false)
      cleanup()
    })

    it('U5b：行内 `.jsr` 入口也把该行的发单价带进弹层（`data-hall-price` → 保存送 `price`）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW, hall_price: '42.00' }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      // 用页面**真实的** templet 造出那一格（不手抄标记，避免与实现分叉）。
      const cols = buildOrderCols(
        () => '原神',
        () => '',
        { isGroupGame: () => false },
      )
      const cell = document.createElement('div')
      cell.innerHTML = (cols.find((c) => c.title === '接手人')!.templet as (d: typeof ROW) => string)({
        ...ROW,
        hall_price: '42.00',
      })
      root.appendChild(cell)
      cell.querySelector<HTMLElement>('a.jsr')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const dialog = runDialogSuccess()
      await flush()
      checkUser(0)

      dialog
        .querySelector<HTMLElement>('[data-act="assign-save"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await vi.waitFor(() => expect(writes()).toHaveLength(1))
      expect((lastRequestConfig().data as Record<string, unknown>).price).toBe('42.00')
      cleanup()
    })

    it('U6：三格是**行内编辑**（点了出 `<input>`、不发请求、不弹 `layer.prompt`）；`Esc` 取消；保存才发一次请求', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW, hall_price: '88.50' }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      await flush()
      const dialog = runDialogSuccess()
      await flush()
      checkUser(0)
      await flush()

      // 三格都能出输入框，且 `Esc` 取消后还原（⛔ 全程零写请求、⛔ 不弹二级窗）
      for (const field of ['price', 'hours', 'project']) {
        const input = beginInlineEdit(field)
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
        expect(
          takerRowEls()[0]!.querySelector<HTMLElement>(`[data-cell="${field}"]`)!.querySelector('input'),
        ).toBeNull()
      }
      expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
      expect(writes()).toHaveLength(0)

      // 行内提交（`blur` 一格、`Enter` 一格）也**不发请求**——只改内存，等「保存指派」。
      editCellInline('price', '77.70')
      editCellInline('hours', '12', 'enter')
      expect(takerRowEls()[0]!.querySelector<HTMLElement>('[data-cell="price"]')!.textContent).toBe(
        '77.70',
      )
      expect(takerRowEls()[0]!.querySelector<HTMLElement>('[data-cell="hours"]')!.textContent).toBe(
        '12',
      )
      expect(writes()).toHaveLength(0)

      dialog
        .querySelector<HTMLElement>('[data-act="assign-save"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await vi.waitFor(() => expect(writes()).toHaveLength(1))
      expect(writes()).toHaveLength(1)
      cleanup()
    })

    it('U6b：提交后**立刻点另一格**，第二格第一次点击就进编辑态（值真的变了也不吞）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW, hall_price: '88.50' }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      await flush()
      runDialogSuccess()
      await flush()
      checkUser(0)
      await flush()

      // 在 price 格里改一个**真的不同**的值，然后不按回车、直接把鼠标点去 hours 格。
      const input = beginInlineEdit('price')
      input.value = '77.70'
      const hoursInput = clickThroughTo(input, 'hours')

      expect(
        hoursInput,
        'price 提交后的第一次点击被吞了：hours 格没有进编辑态（要点两次才进）',
      ).not.toBeNull()
      // 未被保存的值不许丢：提交后 price 格显示的就是刚改的 77.70。
      expect(takerRowEls()[0]!.querySelector<HTMLElement>('[data-cell="price"]')!.textContent).toBe(
        '77.70',
      )
      cleanup()
    })

    it('U6c（对照）：值**没变**时点另一格也不吞（提交仍会把下一格摘掉的那条路径）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      // 「输入框里的当前值 = 88.50」靠**单笔预填** ⇒ 从行内 `.jsr` 入口驱动。
      openViaInline(root, { ...ROW, hall_price: '88.50' })
      await flush()
      runDialogSuccess()
      await flush()
      checkUser(0)
      await flush()

      // 什么都不改（输入框里就是当前值 88.50），直接点去 hours 格。
      const input = beginInlineEdit('price')
      expect(input.value).toBe('88.50')
      const hoursInput = clickThroughTo(input, 'hours')

      expect(
        hoursInput,
        '值没变时第一次点击也被吞了：hours 格没有进编辑态（要点两次才进）',
      ).not.toBeNull()
      expect(takerRowEls()[0]!.querySelector<HTMLElement>('[data-cell="price"]')!.textContent).toBe(
        '88.50',
      )
      cleanup()
    })

    it('U7：用户职位渲染**接口返回的 roles**（合成 3 个 + 「全部」）；那份里没有「代练员」⇒ 默认「全部」', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue({
        items: [],
        truncated: false,
        roles: ACCOUNTS_WITHOUT_TAKER_ROLE.roles,
      } as never)
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      await flush()
      const dialog = runDialogSuccess()
      await flush()

      const roles = [...dialog.querySelectorAll<HTMLInputElement>('input[name="userRole"]')]
      expect(roles.map((r) => r.value)).toEqual(['', ...ACCOUNTS_WITHOUT_TAKER_ROLE.roles])
      expect(roles.map((r) => r.getAttribute('title'))).toEqual([
        '全部',
        ...ACCOUNTS_WITHOUT_TAKER_ROLE.roles,
      ])
      // V-G：roles 里没有「代练员」⇒ 默认档静默回退到「全部」，且**不为这件事**在 console 留痕。
      expect(roles.filter((r) => r.checked).map((r) => r.value)).toEqual([''])
      const roleNoise = warn.mock.calls.filter((args) =>
        args.some((a) => String(a).includes('代练员')),
      )
      expect(roleNoise).toEqual([])
      // 回退之后名单请求也按新选中档走（不带 `role` 键）——否则列表还是按一个不存在的职位筛的。
      const lastParams = lastCall(vi.mocked(fetchAssigneeOptions))[0] as Record<string, unknown>
      expect('role' in lastParams).toBe(false)
      warn.mockRestore()
      cleanup()
    })

    it('U7b：roles 里有「代练员」⇒ 默认选中它，且名单请求就带 `role=代练员`', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      await flush()
      const dialog = runDialogSuccess()
      await flush()

      const roles = [...dialog.querySelectorAll<HTMLInputElement>('input[name="userRole"]')]
      expect(roles.filter((r) => r.checked).map((r) => r.value)).toEqual(['代练员'])
      expect(fetchAssigneeOptions).toHaveBeenLastCalledWith(
        expect.objectContaining({ role: '代练员', limit: 200 }),
      )
      cleanup()
    })

    it('U7c：第一趟名单请求**不猜职位名**（不存在的 `role` 会被后端整条拒 ⇒ V-G 的回退永远发生不了）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      await flush()

      const calls = vi.mocked(fetchAssigneeOptions).mock.calls.map(
        (c) => c[0] as Record<string, unknown>,
      )
      // 第一趟：roles 还不知道 ⇒ **不带** `role`（带一个不存在的名字 = 200+1000，连 roles 都拿不回来）
      expect(calls.length).toBeGreaterThan(0)
      expect('role' in calls[0]!).toBe(false)
      // 拿到 roles 之后补的那一趟才把默认档的筛子加上
      expect((lastCall(vi.mocked(fetchAssigneeOptions))[0] as Record<string, unknown>).role).toBe(
        '代练员',
      )
      cleanup()
    })

    it('U8：§5-B 的逐字项（form / 三处 lay-filter / 两个 legend 与红字 span / 两颗按钮 / 右表三格 `<a>` / 移除的 `lay-event`）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW, hall_price: '88.50' }] })
      vi.mocked(fetchAssigneeOptions).mockResolvedValue(ACCOUNTS as never)
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      const dialog = runDialogSuccess()
      await flush()

      // B1：form 的 class 与 style 逐字（对标 `raw/assignation.html:111`）
      const form = dialog.querySelector<HTMLFormElement>('form')!
      expect(form.className).toBe('layui-form layui-form-pane')
      expect(form.getAttribute('style')).toBe('padding:10px')

      // B2 / B3 / B4：三处 lay-filter
      const roleInputs = [...dialog.querySelectorAll<HTMLInputElement>('input[name="userRole"]')]
      expect(roleInputs.length).toBeGreaterThan(0)
      for (const r of roleInputs) expect(r.getAttribute('lay-filter')).toBe('userRole')
      const bussInputs = [...dialog.querySelectorAll<HTMLInputElement>('input[name="bussType"]')]
      expect(bussInputs).toHaveLength(3)
      for (const b of bussInputs) expect(b.getAttribute('lay-filter')).toBe('bussTypechange')
      const sech = dialog.querySelector<HTMLElement>('button.sechbtn')!
      expect(sech.getAttribute('lay-filter')).toBe('seachBtn')
      // B4：搜索按钮外包 `div.layui-inline > div.layui-input-inline`
      expect(sech.parentElement!.className).toBe('layui-input-inline')
      expect(sech.parentElement!.parentElement!.className).toBe('layui-inline')

      // B5：左列容器 `.user` 的 style 逐字
      expect(dialog.querySelector<HTMLElement>('div.user')!.getAttribute('style')).toBe(
        'margin-left:0px',
      )

      // B6 / B7：两个 legend 各带 `font-size:14px`；红字段是 **legend 内**的 span
      const legends = [...dialog.querySelectorAll<HTMLElement>('legend')]
      expect(legends).toHaveLength(2)
      expect(legends.map((l) => l.getAttribute('style'))).toEqual([
        'font-size:14px',
        'font-size:14px',
      ])
      expect(legends[0]!.textContent).toBe('当前订单分配打手')
      const red = legends[1]!.querySelector<HTMLElement>('span')!
      expect(red.getAttribute('style')).toBe('color:#ff0000;font-weight:600')
      expect(red.textContent).toBe(
        '【↓中途换打手请从下面列表中把没用的账单删除，这关系到后面与代练结算↓】',
      )

      // B10：底部两颗按钮的 `id` / `lay-filter`（文案照旧，⛔ 不许改）
      const save = dialog.querySelector<HTMLElement>('#saveBtn')!
      expect(save.getAttribute('lay-filter')).toBe('saveBtn')
      expect(save.textContent).toBe('保存指派(没改动请勿点)')
      const clear = [...dialog.querySelectorAll<HTMLElement>('button')].find(
        (b) => b.textContent === '清除当前所有接手人',
      )!
      expect(clear.getAttribute('lay-filter')).toBe('delBtn')
      expect(clear.id).toBe('')

      // B8 / B9：勾一人后右表三格渲染成对标的 `<a>`；「移除」带 `lay-event="delAss"`
      checkUser(0)
      const cells = [...takerRowEls()[0]!.querySelectorAll<HTMLElement>('[data-cell]')]
      expect(cells.map((c) => c.querySelector('a')!.className)).toEqual(['t0', 't1', 't2'])
      const t0 = cells[0]!.querySelector<HTMLElement>('a')!
      expect(t0.getAttribute('style')).toBe('color:#F3723A')
      expect(t0.getAttribute('title')).toBe('预计给打手一的发单价，点击可以编辑')
      // B8 负判据：橙色**只**在 `t0` 那一格；`t1`/`t2` 连 `style` 属性都没有
      // （只正面钉 `t0` 的话，把 `color:#F3723A` 铺到三格时判据仍绿）。
      const t1 = cells[1]!.querySelector<HTMLElement>('a')!
      const t2 = cells[2]!.querySelector<HTMLElement>('a')!
      expect(t1.getAttribute('style')).toBeNull()
      expect(t2.getAttribute('style')).toBeNull()
      const remove = takerRowEls()[0]!.querySelector<HTMLElement>('[data-act="assign-remove"]')!
      expect(remove.getAttribute('lay-event')).toBe('delAss')
      cleanup()
    })
  })

  /* ═══════════ 改类型（证据件那个 `.typebox` 弹层 → 批量端点） ═══════════ */

  /**
   * 本组**复用外层 describe 的挂载级 harness**（`LAYUI` 替身 / `mountPage` / `ROW` / `flush` /
   * `lastOpenOpts` / `beforeEach` 的替身重置与 fetch 桩），所以嵌在它里面而不是另起一个顶层
   * describe（另起就得把那一整套再抄一遍）。
   *
   * ⚠️ 工具栏「改类型」走
   * **批量两步式**（勾 1 笔也是长度 1 的批量，spec §D11/D12），不再打单笔 `PATCH …/<单号>/`。
   * 单笔 PATCH 那一支在**改类型**这条路上已没有 UI 入口（改类型只有工具栏一颗）⇒ 保留为默认值。
   */
  describe('改类型（.typebox 弹层 → 批量端点）', () => {
    /**
     * 两步式成功的最小响应：**两发都用它**（第一发 `dry_run` 全合格 ⇒ 直接执行；第二发只读
     * `applied`）。单号取 `ROW.order_no`（20 位、过形状闸）。
     */
    const BATCH_OK = {
      dry_run: true,
      eligible: [ROW.order_no],
      ineligible: [],
      applied: [ROW.order_no],
    }

    /** 弹层节点：开着时可能已被 layui 搬进 `.layui-layer-content`（`root` 之外）⇒ 文档级优先。 */
    function typebox(): HTMLElement {
      const node =
        document.querySelector<HTMLElement>('.layui-layer-content > .typebox') ??
        document.querySelector<HTMLElement>('.typebox')
      expect(node, '找不到 .typebox').not.toBeNull()
      return node!
    }

    /** 入口：勾**恰好一行** → 点工具栏「改类型」。 */
    async function openViaToolbar(): Promise<{ root: HTMLElement; cleanup: () => void }> {
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
      const { root, cleanup } = await mountPage()
      const btn = root.querySelector<HTMLElement>('button[data-tb="changetypesBtn"]')
      expect(btn, '证据件里找不到 [data-tb=changetypesBtn]').not.toBeNull()
      btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      return { root, cleanup }
    }

    function pick(value: '0' | '1'): void {
      typebox().querySelector<HTMLInputElement>(
        `input[name=selbussType][value="${value}"]`,
      )!.checked = true
    }

    function clickSave(): void {
      typebox()
        .querySelector<HTMLElement>('.settypes')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }

    it('导出的常量钉字面值（E-0024：期望值写字面值，不拿常量断言常量）', () => {
      // 标题逐字钉 `修改订单类型`。
      expect(ORDER_TYPE_DIALOG_TITLE).toBe('修改订单类型')
      // 对标的 radio 值 → 我们的枚举名（`apps/business/models.py` 的 `OrderType`）。
      expect(BUSS_TYPE_TO_ORDER_TYPE).toEqual({ '0': 'BOOST', '1': 'COMPANION' })
    })

    it('证据件里 `.settypes` 的 data-notopen 已摘掉（留着会被 onNotOpenClick 吞成「尚未开放」）', () => {
      expect(EVIDENCE).not.toContain('data-notopen="修改订单类型"')
    })

    it('勾一行点「改类型」→ 开的是证据件里那个 `.typebox` 弹层，参数照对标 orderlist.js:3085-3098', async () => {
      const { root, cleanup } = await openViaToolbar()
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      const opts = lastOpenOpts()
      expect(opts.type).toBe(1)
      expect(opts.title).toBe('修改订单类型')
      expect(opts.skin).toBe('layui-layer-lan')
      expect(opts.anim).toBe(5)
      expect(opts.area).toEqual(['700px', '300px'])
      expect(opts.shade).toEqual([0.3, '#333'])
      expect(opts.shadeClose).toBe(true)
      // content 是**证据件那个节点本身**（经 `layui.$` 包过），不是测试/页面自造的标记。
      expect(opts.content).toBe(root.querySelector('.typebox'))
      // 保存按钮身上不再有 `data-notopen`（否则 root 上那个 onNotOpenClick 会先提示「尚未开放」）。
      expect(
        typebox().querySelector<HTMLElement>('.settypes')!.hasAttribute('data-notopen'),
      ).toBe(false)
      cleanup()
    })

    it('每次打开都清掉上一次的勾选，并把复位同步到 layui 的 radio 皮肤', async () => {
      const { root, cleanup } = await openViaToolbar()
      pick('0')
      expect(
        typebox().querySelector<HTMLInputElement>('input[name=selbussType][value="0"]')!.checked,
      ).toBe(true)
      // 再点一次工具栏 = 「又开了一次」（替身里 layer.open 不搬节点、也不要求先关）。
      root
        .querySelector<HTMLElement>('button[data-tb="changetypesBtn"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(
        typebox().querySelectorAll<HTMLInputElement>('input[name=selbussType]:checked'),
      ).toHaveLength(0)
      // 手动 `form.render('radio')` 的依据（读 vendored `public/layui/layui.js` 的 form 模块）：
      // `render` 取的容器是 `a=O(".layui-form")`、radio 取 `a.find("input[type=radio]")`
      // ⇒ 只覆盖 `.layui-form` **之内**的 radio，而 layer 会把 `.typebox` 搬进
      // `.layui-layer-content`（不再是 `.layui-form` 的子孙）⇒ 复位必须在**搬走之前**做。
      expect(LAYUI.form.render).toHaveBeenCalledWith('radio')
      cleanup()
    })

    it('没选类型就保存 → 提示对标原文「请选择业务类型」，一次请求都不发', async () => {
      const { cleanup } = await openViaToolbar()
      clickSave()
      expect(LAYUI.msg).toHaveBeenCalledWith('请选择业务类型', { time: 1500 })
      expect(mockedRequest).not.toHaveBeenCalled()
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })

    it('选「代练单」(0) 保存 → 批量端点（两步式），载荷只有 `order_type` + 两个批量键；成功关层 + reload 先于 msg', async () => {
      // ⚠️ 工具栏「改类型」走**批量两步式**（勾 1 笔也是长度 1 的批量，
      //    spec §D11/D12）⇒ 「PATCH …/<单号>/、载荷恰好 {order_type}」那条单笔判据不成立。
      //    单笔 PATCH 那一支现在是**不可达**的默认值（改类型没有行内入口），其行为由
      //    `order-orderlist-batch.spec.ts` 的批量判据覆盖。
      mockedRequest.mockResolvedValue(BATCH_OK as never)
      const { cleanup } = await openViaToolbar()
      pick('0')
      clickSave()

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const first = mockedRequest.mock.calls[0]![0] as {
        method?: string
        url?: string
        data?: unknown
      }
      expect(first.method).toBe('post')
      expect(first.url).toBe('/api/business/orders/batch/types/')
      // **恰好**这些键：金额字段在 `UpdateOrderPayload` 类型上已被 `Omit`，不许夹带（spec §9-R7）。
      expect(first.data).toEqual({
        order_type: 'BOOST',
        order_nos: [ROW.order_no],
        dry_run: true,
      })
      const second = mockedRequest.mock.calls[1]![0] as { data?: unknown }
      expect(second.data).toEqual({
        order_type: 'BOOST',
        order_nos: [ROW.order_no],
        dry_run: false,
      })
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('订单类型已更新', { time: 1500 }),
      )
      // 成功才关层（`onDone` 里关），层索引来自本组 `openViaToolbar` 的替身返回值 7。
      expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
      // 顺序判据：`reload()`（= `table.reload`）必须先于成功那句 msg（先弹的会被后来的顶掉）。
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
      const okMsgOrder = LAYUI.msg.mock.calls.findIndex((c) => c[0] === '订单类型已更新')
      expect(reloadOrder).toBeLessThan(LAYUI.msg.mock.invocationCallOrder[okMsgOrder]!)
      cleanup()
    })

    it('选「陪玩单」(1) 保存 → 批量端点 order_type=COMPANION', async () => {
      mockedRequest.mockResolvedValue(BATCH_OK as never)
      const { cleanup } = await openViaToolbar()
      pick('1')
      clickSave()

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const first = mockedRequest.mock.calls[0]![0] as { url?: string; data?: unknown }
      expect(first.url).toBe('/api/business/orders/batch/types/')
      expect(first.data).toEqual({
        order_type: 'COMPANION',
        order_nos: [ROW.order_no],
        dry_run: true,
      })
      cleanup()
    })

    it('保存失败 ⇒ 只弹后端 `msg` 原文，弹层留着（不关层、不刷新）', async () => {
      mockedRequest.mockRejectedValue(new BizError(1002, '订单不存在'))
      const { cleanup } = await openViaToolbar()
      pick('0')
      clickSave()

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在', { time: 1500 }),
      )
      // 关层时机照单笔那一支：**成功才关** ⇒ 失败留层让用户重选。
      expect(dialogCloses()).toEqual([])
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('一行都没勾就点「改类型」⇒ 复用工具栏 0 笔文案（`batchOrderNosOrNull()`），不开弹层', async () => {
      LAYUI.table.checkStatus.mockReturnValue({ data: [] })
      const { root, cleanup } = await mountPage()
      root
        .querySelector<HTMLElement>('button[data-tb="changetypesBtn"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))

      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录', { time: 1500 })
      cleanup()
    })

    it('节点被 layui 搬进 `.layui-layer-content` 之后，点保存照常提交（文档级接管）', async () => {
      mockedRequest.mockResolvedValue(BATCH_OK as never)
      const { cleanup } = await openViaToolbar()
      const node = typebox()
      const layerContent = document.createElement('div')
      layerContent.className = 'layui-layer-content'
      document.body.appendChild(layerContent)
      layerContent.appendChild(node)

      node.querySelector<HTMLInputElement>('input[name=selbussType][value="1"]')!.checked = true
      node
        .querySelector<HTMLElement>('.settypes')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const first = mockedRequest.mock.calls[0]![0] as { data?: unknown }
      expect((first.data as { order_type?: string }).order_type).toBe('COMPANION')
      cleanup()
    })
  })

  /* ═══════════ 重要备注 / 角色名两个点击动作（→ PATCH …/<no>/） ═══════════ */

  /**
   * 同样**复用外层 describe 的挂载级 harness**（`LAYUI` 替身 / `mountPage` / `ROW` /
   * `mockedRequest` / `beforeEach` 的替身复位），所以嵌在它里面。
   *
   * 判据一律写**字面值**（标题 / 上限 / 载荷 / 成功文案）：拿页面导出的常量去断言页面自己的行为，
   * 常量被改坏时不会红（E-0024 自指断言）。
   */
  describe('重要备注 / 角色名两个点击动作（PATCH …/<no>/）', () => {
    /** 这一组的行：`ROW` 缺 `game_role_name` / `remark` 两个键，补上（否则 templet 渲染出 undefined）。 */
    const CELL_ROW = { ...ROW, game_role_name: '老王', remark: '客户要求尽快' }

    /**
     * 用页面**真实的**列 templet 造出那一格再挂回 `root`（不手抄标记，避免与实现分叉）。
     *
     * ⚠️ 必须这么造：表体在本文件里**不会**真渲染（`LAYUI.table.render` 是替身）⇒ 节点得自己挂进
     * `root`，这也正是「点击委托挂在页根上」那件事的判据（挂在格子里的事件冒泡到 root）。
     */
    function attachCell(
      root: HTMLElement,
      field: string,
      row: Partial<typeof CELL_ROW> = {},
    ): HTMLElement {
      const cols = buildOrderCols(
        () => '原神',
        () => '',
        { isGroupGame: () => false },
      )
      const col = cols.find((c) => c.field === field)
      expect(col, `列定义里找不到 field=${field}`).toBeDefined()
      const host = document.createElement('div')
      host.innerHTML = (col!.templet as unknown as (d: typeof CELL_ROW) => string)({
        ...CELL_ROW,
        ...row,
      })
      const el = host.firstElementChild as HTMLElement | null
      expect(el, `field=${field} 的 templet 没有产出节点`).not.toBeNull()
      root.appendChild(el!)
      return el!
    }

    function click(el: HTMLElement): void {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }

    function queryCell(cell: HTMLElement, selector: string): HTMLElement {
      const el = cell.querySelector<HTMLElement>(selector)
      expect(el, `格子里找不到 ${selector}`).not.toBeNull()
      return el!
    }

    /**
     * 最近一次 `layer.prompt` 的入参与回调。替身**不会**调用回调 ⇒ 由用例手动触发
     * （这就是 layui 点「确定」时走的那一步，见 `openRemarkPrompt` 的注释）。
     */
    function lastPrompt(): {
      opts: Record<string, unknown>
      cb: (value: string, index: number) => void
    } {
      const calls = LAYUI.layer.prompt.mock.calls
      expect(calls.length, 'layer.prompt 一次都没调').toBeGreaterThan(0)
      const last = calls[calls.length - 1]!
      return {
        opts: last[0] as Record<string, unknown>,
        cb: last[1] as (value: string, index: number) => void,
      }
    }

    it('导出常量的标题 / 上限逐字取对标（备注 L4642、角色名 L4232）', () => {
      expect(REMARK_PROMPT_TITLE).toBe('修改订单重要备注')
      expect(REMARK_MAX_LENGTH).toBe(500)
      // 角色名标题按「值纪律」取对标 `orderlist.js:4231` 的 layer.prompt 原文，
      // **不是** spec §7.4 概述里那句「修改角色名」（差异登记在页面头「已知差异 23」）。
      expect(ROLE_NAME_PROMPT_TITLE).toBe('编辑订单角色名，若没有请写无')
      expect(ROLE_NAME_MAX_LENGTH).toBe(25)
    })

    it('点 `.orderRemark` ⇒ layer.prompt 的入参照对标 L4642（标题/预填/上限，**不设** btn）', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_remark'))

      expect(LAYUI.layer.prompt).toHaveBeenCalledTimes(1)
      const { opts, cb } = lastPrompt()
      // **恰好**这四个键：对标那处 prompt 没有 `btn` / `yes`（用 layui 默认的「确定/取消」）。
      expect(opts).toEqual({
        formType: 0,
        value: '客户要求尽快',
        title: '修改订单重要备注',
        maxlength: 500,
      })
      expect(typeof cb).toBe('function')
      // 点开框本身不发请求（请求只在点「确定」之后）。
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('点 `.changeRoleName` ⇒ 入参照对标 L4232，预填 `data-rolename`（空串照原值空着）', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_role'))
      expect(lastPrompt().opts).toEqual({
        formType: 0,
        value: '老王',
        title: '编辑订单角色名，若没有请写无',
        maxlength: 25,
      })

      LAYUI.layer.prompt.mockClear()
      click(attachCell(root, '_role', { game_role_name: '' }))
      // 空值那一档的**显示**是「无」，而预填取 `data-rolename`（空串）⇒ 框是空的。
      expect(lastPrompt().opts.value).toBe('')
      cleanup()
    })

    it('备注回调 ⇒ PATCH `<no>/` 载荷**恰好** {remark}；close → reload → msg 顺序', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_remark'))
      lastPrompt().cb('改成顺丰补发', 9)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      const cfg = lastRequestConfig()
      expect(cfg.method).toBe('patch')
      expect(cfg.url).toBe('/api/business/orders/20260920213215233029/')
      // **恰好**一个键：不许把金额或其它列夹带进 PATCH（金额要另走 /price/）。
      expect(cfg.data).toEqual({ remark: '改成顺丰补发' })

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('重要备注已更新', { time: 1500 }),
      )
      // 关的**是 layui 交回来的那个层索引**（用例给 9；写死 0 或恒真值都抓不到）。
      expect(LAYUI.layer.close).toHaveBeenCalledWith(9)
      const closeOrder = LAYUI.layer.close.mock.invocationCallOrder[0]!
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
      const okIndex = LAYUI.msg.mock.calls.findIndex((c) => c[0] === '重要备注已更新')
      expect(closeOrder).toBeLessThan(reloadOrder)
      expect(reloadOrder).toBeLessThan(LAYUI.msg.mock.invocationCallOrder[okIndex]!)
      cleanup()
    })

    /**
     * F1（2026-09-23 收口）Critical 的正题：**带引号的自由文本往返不能丢值**。
     *
     * `remark` 被拼进 `data-remark="…"` 这个 HTML 属性；不转义时 `a"b` 会把属性在第二个引号处
     * 截断，`dataset.remark` 只剩 `a` ⇒ prompt 预填成 `a`、确认后 PATCH `{remark: 'a'}`，
     * 用户原本的备注被**静默改坏**。转义后实体由 `dataset` 解码回原文，往返一致。
     *
     * 判别力（红/绿）：把 `remarkRow` 的 `data-remark` 改回未转义的 `${remark}` ⇒
     * 本用例的两个断言同时变红（预填 `a`、PATCH body `{remark: 'a'}`）。
     */
    it('`remark = a"b`：prompt 预填恰好 `a"b`，确认后 PATCH body 也恰好 `{remark: "a\\"b"}`', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_remark', { remark: 'a"b' }))

      // 属性回读：实体在 dataset 上解码回原串（这里先证明格子本身没被引号截断）。
      const cellEl = queryCell(root, 'a.orderRemark')
      expect(cellEl.dataset.remark).toBe('a"b')
      expect(lastPrompt().opts.value).toBe('a"b')

      lastPrompt().cb('a"b', 9)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      // **恰好**一个键、值就是原文（不是被截断的 `a`）。
      expect(lastRequestConfig().data).toEqual({ remark: 'a"b' })
      cleanup()
    })

    it('角色名回调 ⇒ PATCH 载荷**恰好** {game_role_name}，同一套 close → reload → msg', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_role'))
      lastPrompt().cb('新角色', 11)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      const cfg = lastRequestConfig()
      expect(cfg.method).toBe('patch')
      expect(cfg.url).toBe('/api/business/orders/20260920213215233029/')
      expect(cfg.data).toEqual({ game_role_name: '新角色' })

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('角色名已更新', { time: 1500 }))
      expect(LAYUI.layer.close).toHaveBeenCalledWith(11)
      const closeOrder = LAYUI.layer.close.mock.invocationCallOrder[0]!
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
      const okIndex = LAYUI.msg.mock.calls.findIndex((c) => c[0] === '角色名已更新')
      expect(closeOrder).toBeLessThan(reloadOrder)
      expect(reloadOrder).toBeLessThan(LAYUI.msg.mock.invocationCallOrder[okIndex]!)
      cleanup()
    })

    it('备注失败 ⇒ 只弹后端 `msg`，prompt **不关**、列表**不刷**（controller ruling 2）', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_remark'))
      mockedRequest.mockRejectedValue(new BizError(1002, '订单不存在'))
      lastPrompt().cb('改成顺丰补发', 9)

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在', { time: 1500 }),
      )
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('角色名失败 ⇒ 同样只弹后端 `msg`，不关 prompt（失败后可原框重试）', async () => {
      const { root, cleanup } = await mountPage()
      click(attachCell(root, '_role'))
      mockedRequest.mockRejectedValue(new BizError(1002, '订单不存在'))
      lastPrompt().cb('新角色', 11)

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在', { time: 1500 }),
      )
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('订单截图两颗按钮点 `.upImg` / `.mangeImg` ⇒ 各开一个真弹层（图片存储线 Task 8）', async () => {
      const { root, cleanup } = await mountPage()
      const cell = attachCell(root, '_img')

      // 层参数按**本页自己的** PC 分支取值：上传 `['70%','85%']` + `anim:8`；管理 `['80%','85%']` + `anim:1`。
      click(queryCell(cell, 'a.upImg'))
      // ⚠️ 不用 `calls.at(-1)`：本工程 tsconfig 的 `lib` 目标没有 `Array.prototype.at`（TS2550）。
      const upCalls = LAYUI.layer.open.mock.calls
      const up = upCalls[upCalls.length - 1]?.[0] as Record<string, unknown>
      expect(up['title']).toBe('上传订单截图')
      expect(up['shade']).toEqual([0.3, '#333'])
      expect(up['shadeClose']).toBe(true)
      expect(up['type']).toBe(2)
      expect(up['skin']).toBe('layui-layer-lan')
      expect(up['anim']).toBe(8)
      expect(up['area']).toEqual(['70%', '85%'])
      expect(String(up['content'])).toContain('layui-island/page.html?page=order-screenshots-upload')
      // 开层是纯前端动作 ⇒ 一个 API 请求都不发（`LAYUI.layer.prompt` 那条是旧占位路径的对照）
      expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()

      LAYUI.msg.mockClear()
      click(queryCell(cell, 'a.mangeImg'))
      const mangeCalls = LAYUI.layer.open.mock.calls
      const mange = mangeCalls[mangeCalls.length - 1]?.[0] as Record<string, unknown>
      expect(mange['title']).toBe('查看订单截图')
      // ⚠️ 管理那一支的 `anim` 是 **1**（上传是 8）——⛔ 别抽共用常量
      expect(mange['anim']).toBe(1)
      expect(mange['area']).toEqual(['80%', '85%'])
      expect(String(mange['content'])).toContain(
        'layui-island/page.html?page=order-screenshots-manage',
      )
      // ⛔ 旧占位路径（「尚未开放」）必须**一次都不再走**
      expect(LAYUI.msg).not.toHaveBeenCalledWith('「图片管理」尚未开放', { time: 1500 })
      cleanup()
    })

    /**
     * ⭐ **`openScreenshotLayer` 的单号形状闸。**
     *
     * 姊妹页 `order-myorderlist.spec.ts` 有等价用例（那里点的是 `button.upImg`），
     * 订单管理页这道闸（`order-orderlist.ts:5285`）必须**单独**有覆盖——把「二十位数字」的形状判据
     * 放宽成「任何串都放行」不能靠姊妹页的用例抓到。判据与「订单详情」那条**分开**（不同列 / 文案 / 弹层）。
     */
    it('⛔ 订单截图列的单号形状闸：非 20 位数字 ⇒ **不开层** + 固定文案 + **零请求**', async () => {
      const { root, cleanup } = await mountPage()
      // `escapeHtml` 转义后写进 `data-order`，而 `dataset` 会把实体**解码回** `<`/`>` ⇒
      // 闸看到的正是这份可执行形态的串（与「订单详情」那条同一个理由：`layer.open` 的 title 是 HTML）。
      const cell = attachCell(root, '_img', { order_no: '<img src=x onerror=alert(1)>' })
      LAYUI.open.mockClear()
      LAYUI.msg.mockClear()
      const before = mockedRequest.mock.calls.length
      click(queryCell(cell, 'a.upImg'))

      expect(LAYUI.open, '形状不对却开了层').not.toHaveBeenCalled()
      expect(mockedRequest.mock.calls.length, '拒绝分支发了请求').toBe(before)
      // 提示是**固定文案**（⛔ 不把那份不可信输入拼进来）。
      expect(LAYUI.msg).toHaveBeenCalledWith('订单号格式不正确，无法打开订单截图', {
        time: 1500,
      })
      // 与「订单详情」那道闸**不是同一条**：文案不同、也不该混用。
      expect(LAYUI.msg).not.toHaveBeenCalledWith('订单号格式不正确，无法打开订单详情', {
        time: 1500,
      })
      cleanup()
    })
  })

  /* ═══════════ 订单号右键菜单（4 项，spec §6） ═══════════ */

  /**
   * 同样**复用外层 describe 的挂载级 harness**（`LAYUI` 替身 / `mountPage` / `ROW` / `flush` /
   * `mockedRequest` / `beforeEach` 的替身复位与 fetch 桩），所以嵌在它里面。
   *
   * 判据一律写**字面值**（`'.context-menu-list'` / `'已成功复制订单号：…'` / `'tt-orderlist-contextmenu-style'`）：
   * 拿页面导出的常量去断言页面自己的行为是自指断言，常量被改坏时不会红（E-0024）。
   */
  /**
   * 用页面**真实的**「订单号」列 templet 造出那一格再挂回 `root`（不手抄标记，避免与实现分叉）。
   *
   * ⚠️ 必须这么造：`div.orderLine` 是列 templet **运行期**渲染的，挂载那一刻 DOM 里没有它
   * （`LAYUI.table.render` 在本文件里是替身，不会渲染表体）。
   *
   * ⚠️ **T5 起提到这一层**（原来住在「订单号右键菜单」那个 `describe` 里）：T5 的
   * 「点单号蓝链 ⇒ 原地开层」那组用例也要用它，而两个 `describe` 是兄弟（各自的作用域互不可见）。
   * 提到这一层是**搬位置**、函数体一字未动。
   */
  function attachOrderLine(root: HTMLElement, row: typeof ROW = ROW): HTMLElement {
    const cols = buildOrderCols(
      () => '原神',
      () => '',
      { isGroupGame: () => false },
    )
    const col = cols.find((c) => c.title === '订单号')
    expect(col, '列定义里找不到「订单号」').toBeDefined()
    const host = document.createElement('div')
    host.innerHTML = (col!.templet as (d: typeof ROW) => string)(row)
    const line = host.querySelector<HTMLElement>('.orderLine')
    expect(line, '订单号 templet 没有产出 .orderLine').not.toBeNull()
    root.appendChild(line!)
    return line!
  }

  describe('订单号右键菜单（4 项）', () => {
    /**
     * 右键那一格。`cancelable: true` 是判 `preventDefault()` 的前提——不可取消的事件上
     * `defaultPrevented` 恒为 false，那种断言不是判据。
     */
    function rightClick(el: HTMLElement, x = 10, y = 20): MouseEvent {
      const event = new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
      })
      el.dispatchEvent(event)
      return event
    }

    function menu(): HTMLElement | null {
      return document.querySelector<HTMLElement>('.context-menu-list')
    }

    function clickItem(key: string): void {
      const el = document.querySelector<HTMLElement>(`[data-key="${key}"]`)
      expect(el, `菜单里找不到 [data-key=${key}]`).not.toBeNull()
      el!.click()
    }

    /** 装一个假的 `navigator.clipboard`（jsdom 里它整个不存在，要测成功路径就得自己装）。 */
    function stubClipboard(writeText: (text: string) => Promise<void>): ReturnType<typeof vi.fn> {
      const fn = vi.fn(writeText)
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: fn },
        configurable: true,
      })
      return fn
    }

    afterEach(() => {
      // 装过的剪贴板要摘掉：否则后面的「缺 API」用例看到的是上一个用例的替身（假绿）。
      Reflect.deleteProperty(navigator, 'clipboard')
    })

    it('菜单项恰好 4 项，文案与顺序逐字取对标 orderlist.js:5117-5123', () => {
      expect(CONTEXT_MENU_ITEMS).toHaveLength(4)
      expect(CONTEXT_MENU_ITEMS.map((i) => i.key)).toEqual([
        'viewDetails',
        'copyOrderID',
        'copyOrder',
        'copyOrderToNewOrder',
      ])
      expect(CONTEXT_MENU_ITEMS.map((i) => i.label)).toEqual([
        '查看详情',
        '复制订单编号',
        '复制订单内容',
        '复制成新订单',
      ])
    })

    it('在订单号上右键 → 出菜单：4 个可点项 + 3 条分隔线，定位到鼠标坐标', async () => {
      const { root, cleanup } = await mountPage()
      const line = attachOrderLine(root)
      const event = rightClick(line, 33, 44)

      const list = menu()
      expect(list, '右键后没出菜单').not.toBeNull()
      expect(event.defaultPrevented, '没拦掉浏览器原生菜单').toBe(true)
      // 「恰好 4 项」钉在 DOM 上：分隔线**不带** `context-menu-item`，所以这个计数就是项数。
      expect(
        [...list!.querySelectorAll('.context-menu-item')].map((el) => (el.textContent ?? '').trim()),
      ).toEqual(['查看详情', '复制订单编号', '复制订单内容', '复制成新订单'])
      expect(list!.querySelectorAll('.context-menu-separator')).toHaveLength(3)
      // 对标 9 项里被业务方裁定「不渲染」的那 5 项，一项都不许出现。
      for (const gone of [
        '复制玩家自助完善订单链接',
        '获取玩家上号扫码授权链接',
        '查看订单截图',
        '与接手人QQ对话',
        '获取发群聊用的新单消息',
      ]) {
        expect(list!.textContent).not.toContain(gone)
      }
      // 位置 = 鼠标坐标（对标 `position` 用的就是 pageX/pageY）。
      expect(list!.style.left).toBe('33px')
      expect(list!.style.top).toBe('44px')
      cleanup()
    })

    it('右键不落在订单号那一格 → 不出菜单、也不 preventDefault', async () => {
      const { root, cleanup } = await mountPage()
      const event = rightClick(root)

      expect(menu()).toBeNull()
      expect(event.defaultPrevented).toBe(false)
      cleanup()
    })

    it('右键落在那一格**内部**的蓝链上也算（委托用 closest，不是只看 event.target）', async () => {
      const { root, cleanup } = await mountPage()
      const line = attachOrderLine(root)
      const anchor = line.querySelector<HTMLElement>('a.orderdetails')!
      rightClick(anchor)

      expect(menu(), '点在行内蓝链上没出菜单').not.toBeNull()
      LAYUI.open.mockClear()
      clickItem('viewDetails')
      // 单号取自**外层那一格**（`div.orderLine#order_<no>`），不是锚点自己。
      // T5：这条命令的出口是**原地开层**（`layer.open`），不再是 vue-router。
      expect(lastOpenOpts()['content']).toBe(
        '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
      cleanup()
    })

    it('类名照插件产出（`context-menu-item` + `context-menu-icon` + `-icon-<对标图标名>`），字形用 layui', async () => {
      const { root, cleanup } = await mountPage()
      rightClick(attachOrderLine(root))

      const first = document.querySelector<HTMLElement>('[data-key="viewDetails"]')!
      expect(first.classList.contains('context-menu-item')).toBe(true)
      expect(first.classList.contains('context-menu-icon')).toBe(true)
      // 对标 `orderlist.js:5117` 的 `icon: "paste"`；另外三项是 edit / copy / copy。
      expect(first.classList.contains('context-menu-icon-paste')).toBe(true)
      expect(document.querySelector('[data-key="copyOrderID"]')!.classList.contains('context-menu-icon-edit')).toBe(true)
      expect(document.querySelector('[data-key="copyOrder"]')!.classList.contains('context-menu-icon-copy')).toBe(true)
      expect(
        document.querySelector('[data-key="copyOrderToNewOrder"]')!.classList.contains('context-menu-icon-copy'),
      ).toBe(true)
      // 「已知差异 20」：字形改用 vendored layui 字体（对标那套字体全仓取不到）。
      expect(first.classList.contains('layui-icon-form')).toBe(true)
      expect(
        document.querySelector('[data-key="copyOrderID"]')!.classList.contains('layui-icon-edit'),
      ).toBe(true)
      expect(
        document.querySelector('[data-key="copyOrder"]')!.classList.contains('layui-icon-file'),
      ).toBe(true)
      expect(
        document
          .querySelector('[data-key="copyOrderToNewOrder"]')!
          .classList.contains('layui-icon-template-1'),
      ).toBe(true)
      cleanup()
    })

    it('样式表注入到页根，且值逐字取自对标 jquery.contextmenu.min.css', async () => {
      const { cleanup } = await mountPage()
      const style = document.getElementById('tt-orderlist-contextmenu-style')
      expect(style, '样式表没注入').not.toBeNull()
      // 逐条钉样式值（压缩单行原文）。
      expect(CONTEXT_MENU_STYLE_RULES).toContain('min-width:13em;max-width:26em')
      expect(CONTEXT_MENU_STYLE_RULES).toContain(
        '.context-menu-item.context-menu-hover{color:#fff;cursor:pointer;background-color:#2980b9}',
      )
      expect(CONTEXT_MENU_STYLE_RULES).toContain(
        '.context-menu-separator{padding:0;margin:.35em 0;border-bottom:1px solid #e6e6e6}',
      )
      expect(CONTEXT_MENU_STYLE_RULES).toContain(
        '.context-menu-item{position:relative;-webkit-box-sizing:content-box',
      )
      expect(CONTEXT_MENU_STYLE_RULES).toContain('width:2em')
      expect(CONTEXT_MENU_STYLE_RULES).toContain('color:#2980b9;text-align:center')
      // 字形那条覆盖（对标字体取不到，见「已知差异 20」）。
      expect(CONTEXT_MENU_STYLE_RULES).toContain('font-family:layui-icon')
      // 注入的就是这份常量（按 id 去重）。
      expect(style!.textContent).toBe(CONTEXT_MENU_STYLE_RULES)
      cleanup()
    })

    it('点任意处关菜单', async () => {
      const { root, cleanup } = await mountPage()
      rightClick(attachOrderLine(root))
      expect(menu()).not.toBeNull()

      document.body.click()

      expect(menu()).toBeNull()
      cleanup()
    })

    it('Esc 关菜单', async () => {
      const { root, cleanup } = await mountPage()
      rightClick(attachOrderLine(root))
      expect(menu()).not.toBeNull()

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))

      expect(menu()).toBeNull()
      cleanup()
    })

    it('滚动关菜单（含页内任意容器的滚动：监听带 capture）', async () => {
      const { root, cleanup } = await mountPage()
      rightClick(attachOrderLine(root))
      expect(menu()).not.toBeNull()

      const scroller = document.createElement('div')
      document.body.appendChild(scroller)
      scroller.dispatchEvent(new Event('scroll'))

      expect(menu()).toBeNull()
      cleanup()
    })

    it('悬停加 / 离开摘 `context-menu-hover`（否则那条反白规则永远不生效）', async () => {
      const { root, cleanup } = await mountPage()
      rightClick(attachOrderLine(root))
      const item = document.querySelector<HTMLElement>('[data-key="copyOrderID"]')!

      item.dispatchEvent(new MouseEvent('mouseenter'))
      expect(item.classList.contains('context-menu-hover')).toBe(true)
      item.dispatchEvent(new MouseEvent('mouseleave'))
      expect(item.classList.contains('context-menu-hover')).toBe(false)
      cleanup()
    })

    it('「查看详情」走与点单号蓝链**同一个**出口：原地开层（T5；对标那两处调的是同一个 `viewOrderDetails`）', async () => {
      const { root, cleanup } = await mountPage()
      rightClick(attachOrderLine(root))

      LAYUI.open.mockClear()
      clickItem('viewDetails')

      expect(lastOpenOpts()['content']).toBe(
        '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
      cleanup()
    })

    it('「复制订单编号」复制的是**显示值**：有来源单号就复制来源单号', async () => {
      const { root, cleanup } = await mountPage()
      const writeText = stubClipboard(async () => {})
      rightClick(attachOrderLine(root, { ...ROW, source_order_no: 'TB-001' }))

      clickItem('copyOrderID')

      await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('TB-001'))
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('已成功复制订单号：TB-001到剪贴板', { time: 1500 }),
      )
      cleanup()
    })

    it('「复制订单编号」在来源单号为空时复制系统单号（显示值回落）', async () => {
      const { root, cleanup } = await mountPage()
      const writeText = stubClipboard(async () => {})
      rightClick(attachOrderLine(root, { ...ROW, source_order_no: '' }))

      clickItem('copyOrderID')

      await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('20260920213215233029'))
      cleanup()
    })

    /**
     * ⭐ **显示单号 ≠ 系统单号**：这一格显示的是 `TB-001`，而**只有「复制订单编号」**用它。
     * 其余三个命令（查看详情 / 复制订单内容 / 复制成新订单）必须打**系统内置单号** `order_no`——
     * 后端 `lookup_field` 只认它（来源单号在租户内不唯一，模型只给了普通索引）。
     *
     * ⚠️ 为什么要有这**单独一条**：本文件里**其它**驱动这三个命令的用例都用默认 `ROW`
     * （`source_order_no: ''`，`shownNo === orderNo`）——上方那条 `TB-001` 的用例只验
     * 「复制订单编号」，不碰另外三个命令 ⇒ 把实现里那三处 `target.orderNo` 改成
     * `target.shownNo` **能活过它们**；而**下面这一条**有意把 `source_order_no` 换成 `TB-001`、
     * 让显示值与系统单号**不同**，从而把两者**拆开**——那三处逐个变异都会在这里红
     * （实测见修复回执）。
     */
    it('除「复制订单编号」外的三个命令一律用**系统内置单号**，不受显示值（来源单号）影响', async () => {
      const { root, cleanup } = await mountPage()
      mockedRequest.mockResolvedValue({ content: '正文A', demand: '' } as never)
      const line = attachOrderLine(root, { ...ROW, source_order_no: 'TB-001' })

      // ① 查看详情：**层里装的是系统单号那条详情**（`content` 的 `order_no` = 系统单号）。
      //    ⚠️ **但 `title` 的前缀是显示单号**——对标 `title: viewID + "订单详情"`，
      //    而 `viewID` = `data-viewId` = 那一格**渲染出来的**号（`TB-001`）。这正是
      //    「`viewID` 是调用方传进来的什么」这条要求的判据：把 title 前缀换成 `order_no`
      //    本条必须红（见 T5 变异证明）。
      rightClick(line)
      LAYUI.open.mockClear()
      clickItem('viewDetails')
      const opts = lastOpenOpts()
      expect(opts['content']).toBe(
        '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
      expect(opts['title']).toBe('TB-001订单详情')

      // ② 复制订单内容：详情 GET 打的是系统单号
      rightClick(line)
      clickItem('copyOrder')
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      expect(lastRequestConfig()).toEqual({
        method: 'get',
        url: '/api/business/orders/20260920213215233029/',
      })

      // ③ 复制成新订单：POST 打的也是系统单号（不是 `TB-001`）
      mockedRequest.mockClear()
      rightClick(line)
      clickItem('copyOrderToNewOrder')
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      expect(lastRequestConfig()).toEqual({
        method: 'post',
        url: '/api/business/orders/20260920213215233029/copy',
      })
      cleanup()
    })

    it('「复制订单内容」先取详情、复制的是 content', async () => {
      const { root, cleanup } = await mountPage()
      const writeText = stubClipboard(async () => {})
      mockedRequest.mockResolvedValue({ content: '正文A', demand: '需求B' } as never)
      rightClick(attachOrderLine(root))

      clickItem('copyOrder')

      await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('正文A'))
      // 走的是真 `fetchOrder`（只替了它下面那层 `request`）⇒ 这里断言的是真 URL。
      expect(lastRequestConfig()).toEqual({
        method: 'get',
        url: '/api/business/orders/20260920213215233029/',
      })
      cleanup()
    })

    it('「复制订单内容」在 content 为空时回落 demand（spec C8）', async () => {
      const { root, cleanup } = await mountPage()
      const writeText = stubClipboard(async () => {})
      mockedRequest.mockResolvedValue({ content: '', demand: '需求B' } as never)
      rightClick(attachOrderLine(root))

      clickItem('copyOrder')

      await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('需求B'))
      cleanup()
    })

    it('「复制订单内容」取详情失败 ⇒ 弹后端 `msg` 原文，不写剪贴板', async () => {
      const { root, cleanup } = await mountPage()
      const writeText = stubClipboard(async () => {})
      mockedRequest.mockRejectedValue(new BizError(1000, '订单不存在或不可见'))
      rightClick(attachOrderLine(root))

      clickItem('copyOrder')

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在或不可见', { time: 1500 }),
      )
      expect(writeText).not.toHaveBeenCalled()
      cleanup()
    })

    it('「复制成新订单」打的是 POST /orders/<no>/copy（无尾斜杠），成功后刷新列表 + 提示', async () => {
      const { root, cleanup } = await mountPage()
      mockedRequest.mockResolvedValue({} as never)
      rightClick(attachOrderLine(root))

      clickItem('copyOrderToNewOrder')

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      expect(lastRequestConfig()).toEqual({
        method: 'post',
        url: '/api/business/orders/20260920213215233029/copy',
      })
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('已复制成新订单', { time: 1500 }),
      )
      expect(LAYUI.table.reload).toHaveBeenCalledWith('datatable', expect.anything())
      // 顺序：先 `reload()`（它可能弹「尚未开放筛选」警告）再报成功（同本页改状态/改价的口径）。
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
      const msgOrder = LAYUI.msg.mock.calls.findIndex((c) => c[0] === '已复制成新订单')
      expect(reloadOrder).toBeLessThan(LAYUI.msg.mock.invocationCallOrder[msgOrder]!)
      cleanup()
    })

    it('「复制成新订单」失败 ⇒ 只弹后端 `msg` 原文，不刷新列表', async () => {
      const { root, cleanup } = await mountPage()
      mockedRequest.mockRejectedValue(new BizError(1002, '订单不存在'))
      rightClick(attachOrderLine(root))

      clickItem('copyOrderToNewOrder')

      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在', { time: 1500 }))
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('剪贴板 API 缺失 ⇒ 提示失败文案（对标 orderlist.js:4964），不静默', async () => {
      const { root, cleanup } = await mountPage()
      // jsdom 里本来就没有 `navigator.clipboard`；显式钉一次，免得将来的环境变了这条静默失效。
      expect(navigator.clipboard).toBeUndefined()
      rightClick(attachOrderLine(root, { ...ROW, source_order_no: 'TB-001' }))

      clickItem('copyOrderID')

      // ⭐ B2起这条提示是**异步**的：先试 `navigator.clipboard`（没有），
      // 再兜底 `document.execCommand`（jsdom 里也没有）⇒ 结论落在一个 promise 之后。
      // 判据是「一定有人提示这句话」，不是「同步提示」。
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('复制订单号失败了，请手动复制', { time: 1500 }),
      )
      cleanup()
    })

    it('`writeText` 被拒（非安全上下文会这样）⇒ 同样提示失败文案，不静默', async () => {
      const { root, cleanup } = await mountPage()
      stubClipboard(async () => {
        throw new Error('NotAllowedError')
      })
      rightClick(attachOrderLine(root, { ...ROW, source_order_no: 'TB-001' }))

      clickItem('copyOrderID')

      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('复制订单号失败了，请手动复制', { time: 1500 }),
      )
      cleanup()
    })

    it('卸载：菜单节点移除，挂载期加在 document 上的监听一个不剩', async () => {
      const addSpy = vi.spyOn(document, 'addEventListener')
      const removeSpy = vi.spyOn(document, 'removeEventListener')
      try {
        const { root, cleanup } = await mountPage()
        const line = attachOrderLine(root)
        rightClick(line)
        expect(menu(), '右键后没出菜单').not.toBeNull()

        // 挂载期往 document 上装的三个关菜单监听（click / keydown / scroll）。
        // ⚠️ **本清单是「挂载期」的、⛔ 不是「全页所有 document 监听」**：本页另有若干**懒绑定**
        //    的文档级捕获（发价 / 退款 / 改类型 / 收款状态 / 指派的保存按钮），它们只在**弹层打开期间**
        //    挂上、`end` 里摘掉 ⇒ 挂载刚结束时不该出现在这里。⚠️ 价格口径 `3` 档那一层的「搜索」
        //    更进一步：它**绑在按钮元素自身**（开层绑、关层摘），连弹层打开期间都不占文档级监听
        //    ——理由见 `order-orderlist.ts` 的 `onComboPriceSearchClick` 注释（⛔ 别「顺手」把它
        //    改成 `$(document).on(...)` 那种对标写法，那会让本行红，而它红了正是要你回来看这段）。
        const added = addSpy.mock.calls.filter((c) =>
          ['click', 'keydown', 'scroll'].includes(String(c[0])),
        )
        expect(added.map((c) => String(c[0])).sort()).toEqual(['click', 'keydown', 'scroll'])

        cleanup()

        expect(menu(), '卸载后菜单还挂在 body 上').toBeNull()
        const removed = new Set(removeSpy.mock.calls.map((c) => c[1]))
        for (const [, fn] of added) {
          expect(removed.has(fn), '有 document 级监听没在卸载时摘掉').toBe(true)
        }
        // 摘干净了才成立：把那一格挂回去再右键，菜单不会冒出来（root 上的委托也没了）。
        root.appendChild(line)
        rightClick(line)
        expect(menu()).toBeNull()
      } finally {
        addSpy.mockRestore()
        removeSpy.mockRestore()
      }
    })
  })

  /* ═══════════ T5：点单号 ⇒ 原地开层装订单详情复刻页 ═══════════ */

  describe('T5：单号蓝链 `a.orderdetails` ⇒ 原地开层（对标 `OrderList20191212.js` L3755-3767）', () => {
    /** 把那一列的**真 templet 产物**挂进页面，点里面的真元素，返回 `layer.open` 的入参。 */
    async function clickOrderLink(row: typeof ROW = ROW): Promise<Record<string, unknown>> {
      const { root, cleanup } = await mountPage()
      const line = attachOrderLine(root, row)
      const anchor = line.querySelector<HTMLElement>('a.orderdetails')
      expect(anchor, '订单号 templet 没有产出 a.orderdetails').not.toBeNull()
      // 挂载本身不开层（首屏取数走 `table.render`）⇒ 清一次，下面的判据才是钉**点击这一下**。
      LAYUI.open.mockClear()
      anchor!.click()
      try {
        expect(LAYUI.open, '点了单号蓝链没有开层').toHaveBeenCalledTimes(1)
      } finally {
        cleanup()
      }
      return lastOpenOpts()
    }

    it('⭐ 层参数逐字（title / shade / shadeClose / maxmin:false / type / skin / anim / area = **本页自己的** `85%×92%`）', async () => {
      const opts = await clickOrderLink()
      // 对标原话：`title: viewID + "订单详情"`；`ROW.source_order_no` 为空 ⇒ 显示值 = 系统单号。
      expect(opts['title']).toBe('20260920213215233029订单详情')
      expect(opts['shade']).toEqual([0.3, '#333'])
      expect(opts['shadeClose']).toBe(true)
      // ⚠️ **本页对标显式写了 `maxmin: false`**（L3760）——`myemp.js` / `billsummary.js`
      //    那两页的函数体里**没有**这个键 ⇒ 别把「没写」当成「都一样」。
      expect(opts['maxmin']).toBe(false)
      expect(opts['type']).toBe(2)
      expect(opts['skin']).toBe('layui-layer-lan')
      expect(opts['anim']).toBe(8)
      // ⛔ `area` 是**本页自己的值**：同一个产品里我的订单 PC / 我的绩效 / 绩效账单 /
      //    绩效管理页各不相同（`85%×92%` / `85%×92%` / `60%×92%` / `1125px|80%×90%` / `90%×92%`）。
      //    改成任何一个别页的值，本条必须红。
      expect(opts['area']).toEqual(['85%', '92%'])
    })

    it('⭐ `content` = 我们自己的复刻页（`page=order-orderview` + `order_no` = 那一行的系统单号）', async () => {
      const opts = await clickOrderLink()
      // 尖括号里的 `<BASE_URL>` 在本工程是 `/`（vitest 的 base 与 dev 相同）。
      expect(opts['content']).toBe(
        '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
      // 换个单号再点一次：`content` 跟着**行**走（不是写死的第一个值）。
      const opts2 = await clickOrderLink({ ...ROW, order_no: '20260101010101010101' })
      expect(opts2['content']).toBe(
        '/layui-island/page.html?page=order-orderview&order_no=20260101010101010101',
      )
    })

    it('⭐ 基址用 `import.meta.env.BASE_URL`（⛔ 不写死 `/`）：把它 stub 成 `/sub/` ⇒ `content` 跟着变', async () => {
      // ⚠️ **先挂载、再 stub**：挂载期取证据件的 `fetch` 也走 `BASE_URL`，提前改会让那条路径取错文件
      //    （本用例要钉的是**点击**那一刻拼出来的 `content`）。
      const { root, cleanup } = await mountPage()
      const anchor = attachOrderLine(root).querySelector<HTMLElement>('a.orderdetails')!
      vi.stubEnv('BASE_URL', '/sub/')
      try {
        LAYUI.open.mockClear()
        anchor.click()
        // 期望值是**字面值**（E-0024）——不是从 `import.meta.env` 反读回来的 ⇒ **不是自指断言**：
        // 把实现里的 `${import.meta.env.BASE_URL}` 写死成 `'/'`，本条会红（见收口记录的变异 ⑩）。
        expect(lastOpenOpts()['content']).toBe(
          '/sub/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
        )
      } finally {
        vi.unstubAllEnvs()
        cleanup()
      }
    })

    it('⭐ `title` 的前缀是**显示单号**（对标 `viewID` = `data-viewId`），而 `content` 用系统单号', async () => {
      // 这一条把两个号**拆开**：显示值 `TB-001` ≠ 系统单号。判据不是「两个都等于 order_no」。
      const opts = await clickOrderLink({ ...ROW, source_order_no: 'TB-001' })
      expect(opts['title']).toBe('TB-001订单详情')
      expect(opts['content']).toBe(
        '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
    })

    it('⭐ 开层**不发任何 API 请求**（数据由层里的 island 页自己取；本页只开层）', async () => {
      // ⚠️ 必须**先挂载完**再量：`before` 量在挂载之前会把「挂载的请求」算到「点击」头上
      //    （今天恰好是 0 发，这条判据是为了在将来挂载期真发请求时仍然成立）。
      const { root, cleanup } = await mountPage()
      const anchor = attachOrderLine(root).querySelector<HTMLElement>('a.orderdetails')!
      const before = mockedRequest.mock.calls.length
      anchor.click()
      expect(mockedRequest.mock.calls.length).toBe(before)
      cleanup()
    })

    it('⭐ `title` 是 HTML 上下文 ⇒ 前缀那一道 `escapeHtml` **才是承重的**（`dataset` 会把实体解码回 `<`）', async () => {
      const { root, cleanup } = await mountPage()
      const anchor = attachOrderLine(root).querySelector<HTMLElement>('a.orderdetails')!
      // 夹具是**带标记的显示单号**（`data-viewId` 就是 title 前缀的来源）。⚠️ 单号本身仍是
      // 20 位数字 ⇒ 形状闸放行，这一条测的是 `title` 那道转义、不是形状闸。
      // ⚠️ 属性名 HTML 解析后是小写 ⇒ `data-viewid`（`setAttribute('data-viewId')` 也会被
      //    HTML 文档规范化成小写，两种写法都行）。
      anchor.setAttribute('data-viewid', '<b>x</b>&"')
      LAYUI.open.mockClear()
      anchor.click()
      const title = lastOpenOpts()['title']
      // 期望值写字面值（E-0024）：`<` `>` `&` `"` 四个都换掉，末尾接中文「订单详情」。
      expect(title).toBe('&lt;b&gt;x&lt;/b&gt;&amp;&quot;订单详情')
      // 反例：**未转义**那份一个都不许在。为什么必须有这道转义：`link.dataset.*` 会把
      // attribute 实体解码回 `<`/`>`，而 vendored `layui.js` 是把 `a.title`
      // **字符串拼进** `<div class="layui-layer-title">' + a.title + '</div>` 的
      // ⇒ 到了 `layer.open` 这一层，只有**在传参前**再过一次 `escapeHtml` 才拦得住。
      expect(title).not.toContain('<b>')
      cleanup()
    })

    it('⛔ 开层前的**单号形状闸**：非 20 位数字 / 带标记的 `data-id` ⇒ **不开层** + 固定文案提示 + **零请求**', async () => {
      const { root, cleanup } = await mountPage()
      // 四种「形状不对」：短一位 / 多一位 / 非数字 / 带可执行形态的标记串。
      // ⚠️ 最后一种正是 `escapeHtml` 在这条链上挡不住的那一类——`dataset` 会把 attribute
      //    实体**解码回** `<` / `>`，而 `layer.open` 的 `title` 是 HTML 上下文。
      const badShapes = [
        '1234567890123456789',
        '123456789012345678901',
        'abcdefghijklmnopqrst',
        '<img src=x onerror=alert(1)>',
      ]
      LAYUI.open.mockClear()
      LAYUI.msg.mockClear()
      const line = attachOrderLine(root)
      const anchor = line.querySelector<HTMLElement>('a.orderdetails')!
      // 拒绝分支也**不许发请求**（与放行分支同口径）：量的是四次点击前后的差。
      const before = mockedRequest.mock.calls.length
      for (const value of badShapes) {
        anchor.setAttribute('data-order', value)
        anchor.click()
      }
      expect(LAYUI.open, '形状不对却开了层').not.toHaveBeenCalled()
      expect(mockedRequest.mock.calls.length, '拒绝分支发了请求').toBe(before)
      // 提示是**固定文案**（⛔ 不把那份不可信的输入拼进来）；四次点击 = 四条提示。
      expect(LAYUI.msg).toHaveBeenCalledWith('订单号格式不正确，无法打开订单详情', {
        time: 1500,
      })
      expect(LAYUI.msg).toHaveBeenCalledTimes(badShapes.length)
      cleanup()
    })
  })

  /* ═══════════ 统计句的写 DOM / 隐藏（挂载级） ═══════════ */

  describe('工具栏统计值的显示与隐藏（spec §7.2）', () => {
    /**
     * 模拟 layui 渲染表格工具栏：把 `#toolbarDemo` 这个 `<script type="text/html">` 模板的**文本**
     * 灌进 `.layui-table-tool-temp`。
     *
     * 真 layui 就是这么干的（读码：`public/layui/layui.js` 偏移 277397 的视图模板里是
     * `<div class="layui-table-tool"><div class="layui-table-tool-temp">…`；偏移 284626 的
     * `a=o.layTool.find(".layui-table-tool-temp")` 紧接
     * `"string"==typeof e.toolbar && (t=N(e.toolbar).html()||"") && a.html(f(t).render(e))`），
     * 而 jsdom 里跑不了真 layui（`table.render` 是替身）⇒ 这一段由本函数代劳。
     *
     * ⚠️ 统计句那个 span **只存在于渲染后的工具栏里**：`#toolbarDemo` 是 script 模板，
     * `root.querySelector('#toolbarDemo .total')` 恒为空——script 的内容是**文本**，不是子元素。
     */
    function renderLayuiToolbar(root: HTMLElement): void {
      const tpl = root.querySelector<HTMLScriptElement>('script#toolbarDemo')
      expect(tpl, '证据件里找不到 script#toolbarDemo 模板').not.toBeNull()
      const tool = document.createElement('div')
      tool.className = 'layui-table-tool-temp'
      tool.innerHTML = tpl!.textContent ?? ''
      root.appendChild(tool)
    }

    /** 渲染后的统计句 span（证据件整份里 `class="total"` 只出现一次）。 */
    function totalSpan(root: HTMLElement): HTMLElement {
      const el = root.querySelector<HTMLElement>('.layui-table-tool-temp .total')
      expect(el, '渲染后的工具栏里找不到 .total').not.toBeNull()
      return el!
    }

    /**
     * 模拟 layui 的**一次取数**：先 `parseData(响应体)`，再把它的产物交给 `done(res, curr, count)`。
     *
     * 读码（`public/layui/layui.js`，压缩单行）：偏移 292930 的 `success` 里
     * `e = "function"==typeof o.parseData ? o.parseData(e)||e : e`，随后 `done` 由偏移 292034 的
     * `o.done(e, i, e[r.countName], t)` 调起——这个 `e` **就是 `parseData` 的产物**，
     * 所以统计块只能经闭包从 `parseData` 带到 `done`（返回值里多塞键 `done` 也拿不到）。
     */
    function fetchFrame(body: Record<string, unknown>, curr = 1, count = 0): void {
      const opts = lastRenderOpts()
      expect(opts.parseData, 'table.render 没给 parseData').toBeTypeOf('function')
      const parsed = opts.parseData!(body)
      expect(opts.done, 'table.render 没给 done').toBeTypeOf('function')
      opts.done!(parsed, curr, count)
    }

    it('`parseData` 只返回 layui 认得的 4 个键（统计块经闭包带出，不塞进返回值）', async () => {
      const { cleanup } = await mountPage()
      const parsed = lastRenderOpts().parseData!({
        code: 0,
        msg: '',
        data: { items: [ROW], total: 1, summary: SUMMARY },
      })
      expect(Object.keys(parsed as Record<string, unknown>).sort()).toEqual([
        'code',
        'count',
        'data',
        'msg',
      ])
      cleanup()
    })

    it('拿到真 summary ⇒ 那句话按后端聚合值写进 `.total`（不拿当前页那几行自己算）', async () => {
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      const span = totalSpan(root)
      // 渲染出来的第一手内容是证据件那句静态兜底（数字恒 0）⇒ 证明上面灌进来的确实是模板原文。
      expect(span.textContent).toBe(SUMMARY_ZERO_TEXT)

      // 当前页**只有 1 行**，而后端聚合说 12 笔：拿 rows 自己数的实现会写「共1笔」。
      fetchFrame({ code: 0, msg: '', data: { items: [ROW], total: 12, summary: SUMMARY } }, 1, 12)

      expect(span.textContent).toBe(SUMMARY_TEXT)
      expect(span.style.display).not.toBe('none')
      // 8 个状态段都渲染成**可点**的 `.statustab`（不是「看着能点、点了没反应」的假按钮），
      // 且都落在**统计句那个 span 里**（别处的同名节点不算）。
      const tabs = Array.from(span.querySelectorAll<HTMLElement>('.statustab'))
      expect(tabs).toHaveLength(8)
      expect(tabs.map((t) => t.getAttribute('data-status'))).toEqual([
        'UNASSIGNED',
        'WAITING_GRAB',
        'IN_PROGRESS',
        'PENDING_REVIEW',
        'COMPLETED',
        'PAUSED',
        'ISSUE',
        'REFUNDED',
      ])
      expect(tabs[0]!.getAttribute('style')).toBe('cursor:pointer')
      expect(tabs[0]!.getAttribute('lay-tips')).toBe('点击可以快速筛选未分配的订单')
      // `title` 是**有意新增**（本页 vendored layui 里 `lay-tips` 不渲染，见文件头「已知差异 57」）
      // ——同族先例见「已知差异 24」的 V1。
      expect(tabs[0]!.getAttribute('title')).toBe('点击可以快速筛选未分配的订单')
      cleanup()
    })

    it('点 `.statustab` ⇒ 先清空再设成该档（状态码→中文），最后点搜索那颗按钮', async () => {
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      fetchFrame({ code: 0, msg: '', data: { items: [ROW], total: 12, summary: SUMMARY } }, 1, 12)

      // formSelects 替身：**挂在 `window.layui`**——生产代码经 `@/layui/vendor` 的
      // `getFormSelects()`（读 `window.layui.formSelects`）取它。⛔ 必须在 `mountPage()` **之后**
      // 才挂：挂载期页面自己会 `initFormSelects` / `render`，那两步要的还是「拿不到实例」的现状。
      const value = vi.fn()
      vi.stubGlobal('layui', { formSelects: { value, render: vi.fn() } })

      // 记录「搜索那颗按钮真的被点到」以及**它与写值调用的先后**：本页工具栏搜索出口就是
      // `[data-tb="seachBtnOk"]`（`onToolbarClick` 的 `action === 'seachBtnOk'` ⇒ `reload()`）。
      const searchBtn = root.querySelector<HTMLElement>('[data-tb="seachBtnOk"]')
      expect(searchBtn, '证据件里找不到 [data-tb=seachBtnOk]').not.toBeNull()
      const order: string[] = []
      searchBtn!.addEventListener('click', () => order.push('search'))
      value.mockImplementation((id: string, val?: unknown) => {
        order.push(`value:${id}:${JSON.stringify(val)}`)
        return [] as unknown
      })

      // 「代练中」= `IN_PROGRESS`（**状态码 → 中文 label** 由 `statusLabel()` 现算）。
      const tab = root.querySelector<HTMLElement>('.statustab[data-status="IN_PROGRESS"]')
      expect(tab, '统计句里找不到 data-status=IN_PROGRESS 的状态段').not.toBeNull()
      // ⚠️ **先清一次 `table.reload`**：`done` 在**首帧**会补一发 `reload(1)`（复刻对标
      //    「500ms 后自动搜索一次」，见 `done` 里 `firstDone` 那段）——它属于上面那次 `fetchFrame`，
      //    不是本次点击的产物。不清就会把 1 次点击看成 2 次。
      LAYUI.table.reload.mockClear()
      tab!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

      // ① 两发写值都打在 `orderSta` 这个实例上，且顺序 = 对标那三行：先清空、再设成唯一一项。
      expect(value.mock.calls).toEqual([['orderSta', []], ['orderSta', ['代练中']]])
      // ② 搜索那颗按钮确实被点到，且**发生在写完筛选项之后**（顺序反了筛选就不生效）。
      expect(order).toEqual(['value:orderSta:[]', 'value:orderSta:["代练中"]', 'search'])
      // ③ 落到了本页工具栏那条**既有**分支上（`reload()` → `table.reload`），不是自造一条刷新路径。
      expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
      cleanup()
    })

    it('别处的同名 `.statustab` 不许误伤（点击委托只认统计句那一段）', async () => {
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      fetchFrame({ code: 0, msg: '', data: { items: [ROW], total: 12, summary: SUMMARY } }, 1, 12)
      const value = vi.fn()
      vi.stubGlobal('layui', { formSelects: { value, render: vi.fn() } })

      // 统计句**之外**造一个同名节点（对照：它在 `.total` span 里时上面的用例证明会生效）。
      const outsider = document.createElement('span')
      outsider.className = 'statustab'
      outsider.setAttribute('data-status', 'IN_PROGRESS')
      root.appendChild(outsider)
      // 同上一支：先清掉首帧 `done` 补的那发 `reload(1)`，这条判据只盯「本次点击」。
      LAYUI.table.reload.mockClear()
      outsider.dispatchEvent(new MouseEvent('click', { bubbles: true }))

      expect(value).not.toHaveBeenCalled()
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('summary 为 null（持 order.restrict_view_total_stats）⇒ 那句隐藏', async () => {
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      const span = totalSpan(root)

      fetchFrame({ code: 0, msg: '', data: { items: [], total: 0, summary: null } })

      expect(span.style.display).toBe('none')
      // 隐藏那支**没有**可点段（统计句仍是被隐藏的静态模板文本）⇒ 点了自然没有任何反应。
      expect(span.querySelectorAll('.statustab')).toHaveLength(0)
      cleanup()
    })

    it('隐藏前已经有可点段 ⇒ 隐藏后那批旧段一个都不许再改筛选（判据是代码里的 display 判断）', async () => {
      // ⭐ `renderSummary` 的隐藏支**只改 display、不清 innerHTML**
      //    ⇒ 「先有真统计句、下一帧变 null」时旧的 `.statustab` **仍在 DOM 里**。真鼠标点不到，
      //    但程序化派发的冒泡 click 会命中 ⇒ 单靠 CSS 不是判据。这条如果红，说明
      //    `onSummaryStatusClick` 里那句 `totalEl.style.display === 'none'` 判断被去掉了。
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      const span = totalSpan(root)
      fetchFrame({ code: 0, msg: '', data: { items: [ROW], total: 12, summary: SUMMARY } }, 1, 12)
      const stale = span.querySelector<HTMLElement>('.statustab[data-status="IN_PROGRESS"]')
      expect(stale, '前置：隐藏前应当有可点段').not.toBeNull()

      // 切到「隐藏」那一帧（同一批旧 `.statustab` 原地留下）。
      fetchFrame({ code: 0, msg: '', data: { items: [], total: 0, summary: null } })
      expect(span.style.display).toBe('none')

      const value = vi.fn()
      vi.stubGlobal('layui', { formSelects: { value, render: vi.fn() } })
      LAYUI.table.reload.mockClear()
      span.querySelector<HTMLElement>('.statustab[data-status="IN_PROGRESS"]')!.dispatchEvent(
        new MouseEvent('click', { bubbles: true }),
      )

      expect(value).not.toHaveBeenCalled()
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })

    it('响应里**根本没有** summary 这个键（大厅响应）⇒ 与 null 同义，隐藏', async () => {
      // `apps/business/api/base.py` 的哨兵让大厅响应连 `summary` 键都没有 ⇒ 缺键必须与 null 同形。
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      const span = totalSpan(root)

      fetchFrame({ code: 0, msg: '', data: { items: [], total: 0 } })

      expect(span.style.display).toBe('none')
      cleanup()
    })

    it('先 null 后拿到真对象 ⇒ 重新显示（同一会话换账号 / 换筛选后可能又可见）', async () => {
      const { root, cleanup } = await mountPage()
      renderLayuiToolbar(root)
      const span = totalSpan(root)

      fetchFrame({ code: 0, msg: '', data: { items: [], total: 0, summary: null } })
      expect(span.style.display).toBe('none')

      fetchFrame({ code: 0, msg: '', data: { items: [ROW], total: 12, summary: SUMMARY } }, 1, 12)
      expect(span.style.display).not.toBe('none')
      expect(span.textContent).toBe(SUMMARY_TEXT)
      cleanup()
    })
  })

  /* ═══════════ T7：接单价列金额链接改读 `data-fee` ═══════════ */

  describe('T7：点接单价金额链接（`.editOrderPrice`）', () => {
    /**
     * 用页面**真实的** `_price` 列 templet 造出那一格，再取**金额链接**（`a.editOrderPrice`）
     * 挂回 `root`。
     *
     * ⚠️ 不能取 `host.firstElementChild`：`priceRow` 先出收款状态标记、后出金额链接
     * （对标 `orderlist.js:2085` 的 `return label + '<a class="editOrderPrice">'`），首个元素是标记。
     * 所以按 **class 选择器**取（这同时就是「class 已改名」的判据）。
     */
    function attachPriceLink(
      root: HTMLElement,
      row: Partial<typeof ROW> = {},
    ): HTMLElement {
      const cols = buildOrderCols(
        () => '原神',
        () => '',
        { isGroupGame: () => false },
      )
      const col = cols.find((c) => c.field === '_price')
      expect(col, '列定义里找不到 field=_price').toBeDefined()
      const host = document.createElement('div')
      host.innerHTML = (col!.templet as unknown as (d: typeof ROW) => string)({
        ...ROW,
        ...row,
      })
      const el = host.querySelector<HTMLElement>('a.editOrderPrice')
      expect(el, 'field=_price 的 templet 没有产出 `a.editOrderPrice`').not.toBeNull()
      root.appendChild(el!)
      return el!
    }

    it('prompt 预填 `data-fee`；确认后 body 恰好 `{ total_price }` 打到既有 `/price/` 出口', async () => {
      const { root, cleanup } = await mountPage()
      const cellEl = attachPriceLink(root, {
        total_price: '88.00',
        payment_status: 'UNSETTLED',
        payment_method: '',
      })
      cellEl.dispatchEvent(new MouseEvent('click', { bubbles: true }))

      expect(LAYUI.layer.prompt).toHaveBeenCalledTimes(1)
      const first = LAYUI.layer.prompt.mock.calls[0]!
      const opts = first[0] as Record<string, unknown>
      // 预填的是 `data-fee`（**不是** `textContent`；两者在本例里恰好同值，见下一条用例）。
      expect(opts.value).toBe('88.00')
      expect(opts.title).toBe('更改接单价，可为0，但不能为空')
      // ⚠️ `openPricePrompt` 设了自定义 `btn` ⇒ 它把「读输入框」的回调放在 `opts.yes` 里，
      // **不是** `layer.prompt` 的第二实参（实测见该函数的注释）。这里照真实形状手工触发。
      ;(opts.yes as (i: number, l: unknown) => void)(3, {
        find: () => ({ val: () => '99.00' }),
      })

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      const cfg = lastRequestConfig()
      expect(cfg.method).toBe('post')
      expect(cfg.url).toBe('/api/business/orders/20260920213215233029/price/')
      expect(cfg.data).toEqual({ total_price: '99.00' })
      cleanup()
    })

    it('点击读的是 `data-fee` 而不是格子文本：`data-fee` 与文本不一致时预填 `data-fee`', async () => {
      const { root, cleanup } = await mountPage()
      // 载荷里的金额是后端原始串；这一条把 `data-fee` 与渲染文本**造得不同**（直接改属性），
      // 「读 textContent」与「读 `data-fee`」两种实现就会预填出不同的值 ⇒ 两者可分辨。
      const cellEl = attachPriceLink(root, { total_price: '12.34' })
      cellEl.setAttribute('data-fee', '56.78')
      cellEl.textContent = '12.34'
      cellEl.dispatchEvent(new MouseEvent('click', { bubbles: true }))

      const opts = LAYUI.layer.prompt.mock.calls[0]![0] as Record<string, unknown>
      expect(opts.value).toBe('56.78')
      cleanup()
    })
  })

  /* ═══════════ T8：发单价两支 + 收款状态弹窗 + 工具栏那两颗 ═══════════ */

  describe('T8：发单价两支的点击 + 收款状态弹窗 + 工具栏那两颗', () => {
    function clickToolbarBtn(root: HTMLElement, tb: string): void {
      const btn = root.querySelector<HTMLElement>(`button[data-tb="${tb}"]`)
      expect(btn, `证据件里找不到 [data-tb=${tb}]`).not.toBeNull()
      btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }

    /**
     * 两步式成功的最小响应（spec §D11/D12）：**两发都用它**——第一发全合格 ⇒ 直接执行；
     * 第二发只读 `applied`。自 Task 4a 起工具栏那两颗（改发单价 / 收款状态）勾 1 笔也走批量。
     */
    function batchOk(orderNo: string): {
      dry_run: boolean
      eligible: string[]
      ineligible: unknown[]
      applied: string[]
    } {
      return { dry_run: true, eligible: [orderNo], ineligible: [], applied: [orderNo] }
    }

    /**
     * 用「接单价」列的真实 templet 渲一行、取 `.jsSta` 并挂回 `root`（行内收款状态入口，
     * 保持**单笔**；工具栏那颗自 Task 4a 起走批量）。返回那个 `.jsSta` 元素。
     */
    function attachJsSta(
      root: HTMLElement,
      row: Record<string, unknown>,
    ): HTMLElement {
      const cols = buildOrderCols(() => '原神', () => '', { isGroupGame: () => false })
      const col = cols.find((c) => c.field === '_price')!
      const host = document.createElement('div')
      host.innerHTML = (col.templet as unknown as (d: Record<string, unknown>) => string)(row)
      const el = host.querySelector<HTMLElement>('.jsSta')
      expect(el, '_price 列的 templet 没有产出 .jsSta').not.toBeNull()
      root.appendChild(el!)
      return el!
    }

    /** 取「发单价」列的真实 templet 渲一行，把产出节点挂回 `root`（点击委托挂在 `root` 上）。 */
    function attachThrowCell(
      root: HTMLElement,
      row: Record<string, unknown>,
    ): HTMLElement {
      const cols = buildOrderCols(() => '原神', () => '', { isGroupGame: () => false })
      const col = cols.find((c) => c.field === '_throw')
      expect(col, '列定义里找不到 field=_throw').toBeDefined()
      const host = document.createElement('div')
      host.innerHTML = (col!.templet as unknown as (d: Record<string, unknown>) => string)(row)
      const el = host.firstElementChild as HTMLElement | null
      expect(el, '发单价列的 templet 没有产出节点').not.toBeNull()
      root.appendChild(el!)
      return el!
    }

    const THROW_ROW = {
      ...ROW,
      hall_price: '66.00',
      hall_publisher_name: 'publisher1',
      payment_status: 'UNSETTLED',
      payment_method: '',
    }

    it('点 `.changeThrowFeeEdit` ⇒ body 恰好 `{ price }` 打到 `/hall-price/`，`50%` 原样送出', async () => {
      const { root, cleanup } = await mountPage()
      const el = attachThrowCell(root, { ...THROW_ROW, hall_publisher_name: '' })
      expect(el.className).toContain('changeThrowFeeEdit')
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))

      const first = LAYUI.layer.prompt.mock.calls[0]!
      expect((first[0] as Record<string, unknown>).title).toBe('更改发单价，可为0，但不能为空')
      expect((first[0] as Record<string, unknown>).maxlength).toBe(10)
      // ⚠️ 偏差登记（与计划 Step 1b 的 `(first[1])(3, {find})` 不同）：`layer.prompt(opts, cb)` 的
      //    第二实参由 layui 的**默认 `yes`** 以 `(value, index, layero)` 调用
      //    （vendored `public/layui/layui.js` 的 `prompt=function(i,n){…n&&n(t,e,a)}`；
      //    本页 `openEditPrompt` 就是这么收值的）。计划的 `(3, {find})` 是 `yes(index, layero)`
      //    的形状，只有「opts 里自传 yes」时才成立——那条路与本函数的实现不一致，见报偏差。
      ;(first[1] as (value: string, index: number) => void)('50%', 3)

      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      const cfg = lastRequestConfig()
      expect(cfg.method).toBe('post')
      expect(cfg.url).toBe('/api/business/orders/20260920213215233029/hall-price/')
      // 前端**不做换算、不做上限校验**：换算在服务端，前端镜像会漂移（spec §8 第 28 条）。
      expect(cfg.data).toEqual({ price: '50%' })
      cleanup()
    })

    it('点 `.changeThrowFee` ⇒ 标题是带 `%` 说明的那一句，body 同样是 `{ price }`', async () => {
      const { root, cleanup } = await mountPage()
      const el = attachThrowCell(root, THROW_ROW)
      expect(el.className).toContain('changeThrowFee')
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const first = LAYUI.layer.prompt.mock.calls[0]!
      expect((first[0] as Record<string, unknown>).title).toBe(
        '改发单价，可为0，但不能为空，带%号代表按接单价比例发单',
      )
      ;(first[1] as (value: string, index: number) => void)('7.00', 3)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      expect(lastRequestConfig().data).toEqual({ price: '7.00' })
      cleanup()
    })

    it('工具栏「改发单价」：恰好一笔 ⇒ prompt 默认值 `0`；0 笔 ⇒ 既有那句提示', async () => {
      LAYUI.table.checkStatus.mockReturnValue({ data: [THROW_ROW] })
      const { root, cleanup } = await mountPage()
      clickToolbarBtn(root, 'changeOrderThrowPriceBtn')
      expect((LAYUI.layer.prompt.mock.calls[0]![0] as Record<string, unknown>).value).toBe('0')

      LAYUI.layer.prompt.mockClear()
      renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], [])
        .forEach((box) => { box.checked = true })
      clickToolbarBtn(root, 'changeOrderThrowPriceBtn')
      expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
      expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录', { time: 1500 })
      cleanup()
    })

    it('工具栏「收款状态」：未选任何 radio 提交 ⇒ `layer.alert` 文案与标题逐字', async () => {
      // ⚠️ 偏差登记（与计划 Step 1b 不同）：计划的 THROW_ROW 带 `payment_status: 'UNSETTLED'`，
      //    而 spec §7.6 要求用该行的码**预勾选** ⇒ 那一版根本到不了「未选」分支。
      //    「未选任何 radio 提交」的前提是「当前码映射不到任何档」⇒ 这里把码改成空串。
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...THROW_ROW, payment_status: '' }] })
      const { root, cleanup } = await mountPage()
      clickToolbarBtn(root, 'setOrderJSBtn')

      // `content` 是证据件里那个 `.setDiv`（页面把证据件整段设为 innerHTML ⇒ 节点已在 DOM 里）
      const opts = lastOpenOpts()
      expect(opts.title).toBe('更改与发单方的结算状态')
      expect(opts.skin).toBe('layui-layer-lan')
      expect(opts.type).toBe(1)
      expect(opts.area).toEqual(['70%', '260px'])
      // 尺寸 / `anim` 逐值取对标（`orderlist.js:4119-4134`）——计划 Step 3c 的片段漏了这三项。
      expect(opts.anim).toBe(5)
      expect(opts.shade).toEqual([0.3, '#333'])
      expect(opts.shadeClose).toBe(true)

      // ⚠️ 文档级取（真 layui 已把节点搬进 `.layui-layer-content`，B-Minor-1）——`root` 里已无它。
      expect(root.querySelector('.setDiv')).toBeNull()
      const dialog = settDialog()
      // 运行期摘掉保存按钮的 `data-notopen`（证据件里已删；这里确认真的没有它）
      expect(dialog.querySelector('[data-notopen]')).toBeNull()
      expect(dialog.querySelector<HTMLInputElement>('input[name="jsst"]:checked')).toBeNull()
      dialog.querySelector<HTMLElement>('[lay-filter="saveSettbtn"]')!.dispatchEvent(
        new MouseEvent('click', { bubbles: true }),
      )
      expect(LAYUI.layer.alert).toHaveBeenCalledWith('请选择一种结算状态！', { title: '提示' })
      cleanup()
    })

    it('工具栏「收款状态」选「已结算」⇒ 批量端点（两步式），body 送码（中文→码在此钉住）', async () => {
      // ⚠️ 工具栏那颗勾 1 笔也是**长度 1 的批量**（spec §D11/D12）⇒
      //    「POST …/<单号>/payment-status/」那条单笔判据不成立。单笔那一支仍由下面
      //    「点行内 `.jsSta`」那条钉（行内入口保持单笔）。
      mockedRequest.mockResolvedValue(batchOk(THROW_ROW.order_no) as never)
      LAYUI.table.checkStatus.mockReturnValue({ data: [THROW_ROW] })
      const { root, cleanup } = await mountPage()
      clickToolbarBtn(root, 'setOrderJSBtn')
      const dialog = settDialog()
      const radio = dialog.querySelector<HTMLInputElement>('input[name="jsst"][value="已结算"]')!
      radio.checked = true
      dialog.querySelector<HTMLElement>('[lay-filter="saveSettbtn"]')!.dispatchEvent(
        new MouseEvent('click', { bubbles: true }),
      )
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const first = mockedRequest.mock.calls[0]![0] as { method?: string; url?: string; data?: unknown }
      expect(first.method).toBe('post')
      expect(first.url).toBe('/api/business/orders/batch/payment-status/')
      expect(first.data).toEqual({
        payment_status: 'SETTLED',
        order_nos: [THROW_ROW.order_no],
        dry_run: true,
      })
      cleanup()
    })

    it('点行内 `.jsSta` ⇒ 对**该行**开弹窗（不需要勾选）', async () => {
      LAYUI.table.checkStatus.mockReturnValue({ data: [] })
      const { root, cleanup } = await mountPage()
      const cols = buildOrderCols(() => '原神', () => '', { isGroupGame: () => false })
      const col = cols.find((c) => c.field === '_price')!
      const host = document.createElement('div')
      host.innerHTML = (col.templet as unknown as (d: Record<string, unknown>) => string)({
        ...ROW,
        total_price: '88.00',
        payment_status: 'SETTLING',
      })
      const sta = host.querySelector<HTMLElement>('.jsSta')!
      root.appendChild(sta)
      sta.dispatchEvent(new MouseEvent('click', { bubbles: true }))

      expect(lastOpenOpts().title).toBe('更改与发单方的结算状态')
      // 预勾选：该行 `payment_status = SETTLING` ⇒ 对应证据件里 `value="结算中"` 那个 radio
      const dialog = settDialog()
      expect(dialog.querySelector<HTMLInputElement>('input[name="jsst"]:checked')?.value).toBe('结算中')
      cleanup()
    })

    it('证据件里那颗保存按钮已摘掉「尚未开放」标注', () => {
      expect(EVIDENCE).not.toContain('data-notopen="更改与发单方的结算状态"')
    })

    it('真 layui 搬节点：开层后节点在 `.layui-layer-content` 且提交走文档级取；关层后搬回 `root` 且 `display:none`；第二次打开提交仍成立（B-Minor-1）', async () => {
      // 判别力：把实现的 `settPanel()` 改回 `root.querySelector('.setDiv')` 这一条会红
      // （节点已搬出 `root` ⇒ 提交时读不到、落「未选」分支、一个请求都不发）。
      //
      // ⚠️ 本条的判据是**弹层机制**（搬节点 / 文档级取 / `end` 搬回），
      //    与「哪颗按钮」无关 ⇒ 改由**行内 `.jsSta`**（保持单笔）驱动，判据一个字不放宽。
      expect(LAYUI.open.getMockImplementation()).toBe(LAYUI.realOpen)
      const { root, cleanup } = await mountPage()
      /** 点行内 `.jsSta` 开层，返回该层的真实索引。 */
      const openOnce = (): number => {
        const sta = attachJsSta(root, { ...THROW_ROW, payment_status: 'SETTLING' })
        sta.dispatchEvent(new MouseEvent('click', { bubbles: true }))
        const results = LAYUI.open.mock.results
        return results[results.length - 1]!.value as number
      }
      /** 在**已搬出 `root`** 的节点里选「已结算」并点保存（保存按钮靠文档级捕获接管）。 */
      const submitSettled = async (): Promise<Record<string, unknown>> => {
        const dialog = settDialog()
        dialog.querySelector<HTMLInputElement>('input[name="jsst"][value="已结算"]')!.checked = true
        dialog
          .querySelector<HTMLElement>('[lay-filter="saveSettbtn"]')!
          .dispatchEvent(new MouseEvent('click', { bubbles: true }))
        await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
        return lastRequestConfig()
      }

      // 第一次：节点已搬出 `root`（`root` 里查不到）⇒ 提交时 `settPanel()` 必须走**文档级**分支。
      const first = openOnce()
      expect(root.querySelector('.setDiv')).toBeNull()
      expect(
        settDialog().querySelector<HTMLInputElement>('input[name="jsst"]:checked')?.value,
      ).toBe('结算中')
      const cfg = await submitSettled()
      expect(cfg.method).toBe('post')
      expect(cfg.url).toBe('/api/business/orders/20260920213215233029/payment-status/')
      expect(cfg.data).toEqual({ payment_status: 'SETTLED' })

      // 成功 ⇒ `layer.close(index)` ⇒ `end()` 把节点搬回 host（`root` 之内）并 `display:none`。
      ;(LAYUI.layer.close as (i: number) => void)(first)
      const back = root.querySelector<HTMLElement>('.setDiv')
      expect(back).not.toBeNull()
      expect(back!.style.display).toBe('none')

      // 第二次打开：仍取得到节点、仍按该行当前的码预勾选，且提交仍成立。
      mockedRequest.mockClear()
      openOnce()
      expect(root.querySelector('.setDiv')).toBeNull()
      expect(
        settDialog().querySelector<HTMLInputElement>('input[name="jsst"]:checked')?.value,
      ).toBe('结算中')
      const cfg2 = await submitSettled()
      expect(cfg2.data).toEqual({ payment_status: 'SETTLED' })
      cleanup()
    })

    /* —— 把两批曾只用手工探针验过的行为固化成用例（判据照 task-8-review §5/§6） —— */

    it('① 多选（>1 笔）⇒ 两颗都开各自弹层（真批量，spec §D11），未提交前零请求', async () => {
      // ⚠️ 勾 >1 笔不再提示「尚未开放」——工具栏那两颗都走**真批量**
      //    （勾 1 笔也是长度 1 的批量）：弹层照开、请求仍等用户确认才发。
      const TWO = [THROW_ROW, { ...THROW_ROW, order_no: '20260920213215233030' }]
      LAYUI.table.checkStatus.mockReturnValue({ data: TWO })
      const { root, cleanup } = await mountPage()

      // 改发单价：勾 2 笔 ⇒ prompt 照开（批量没有「本单值」，预填仍是工具栏默认的 `0`）。
      clickToolbarBtn(root, 'changeOrderThrowPriceBtn')
      expect(LAYUI.layer.prompt).toHaveBeenCalledTimes(1)
      expect(mockedRequest).not.toHaveBeenCalled()

      // 收款状态：勾 2 笔 ⇒ 开同一个弹层；批量没有「本单当前码」⇒ 不预勾选。
      clickToolbarBtn(root, 'setOrderJSBtn')
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      expect(settDialog().querySelector<HTMLInputElement>('input[name="jsst"]:checked')).toBeNull()
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('② 成功 ⇒ 关层 + `reload()` + `msg`；失败 ⇒ 只弹后端 `msg` 且**不关层**（两颗各验一次）', async () => {
      const BATCH_OK_ONE = batchOk(THROW_ROW.order_no)
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [THROW_ROW] })
      const { root, cleanup } = await mountPage()
      /** 「改发单价」那颗走完一次：点工具栏 → 打开 prompt → 确认输入。 */
      const submitThrowPrice = async (value: string): Promise<void> => {
        LAYUI.layer.prompt.mockClear()
        clickToolbarBtn(root, 'changeOrderThrowPriceBtn')
        const cb = LAYUI.layer.prompt.mock.calls[0]![1] as (v: string, i: number) => void
        cb(value, 3)
        await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      }
      /** 「收款状态」那颗走完一次：点工具栏 → 选「已结算」→ 点保存。 */
      const submitPaymentStatus = async (): Promise<void> => {
        clickToolbarBtn(root, 'setOrderJSBtn')
        // ⚠️ 本条把 `layer.open` 固定成 `() => 7`（要一个确定的层索引来断 `close(7)`）
        //    ⇒ 替身**不搬节点**，节点仍在 `root` 里，所以这里用 `root` 级取；
        //    「搬节点 + 文档级取 + 搬回」由上面那条 B-Minor-1 用例覆盖。
        const dialog = root.querySelector<HTMLElement>('.setDiv')!
        dialog.querySelector<HTMLInputElement>('input[name="jsst"][value="已结算"]')!.checked = true
        dialog
          .querySelector<HTMLElement>('[lay-filter="saveSettbtn"]')!
          .dispatchEvent(new MouseEvent('click', { bubbles: true }))
        await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalled())
      }

      // 改发单价：成功（两步式全合格）⇒ `onDone` 里 close(3) + reload + msg
      mockedRequest.mockResolvedValue(BATCH_OK_ONE as never)
      await submitThrowPrice('88.00')
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('发单价已更新', { time: 1500 }))
      expect(LAYUI.layer.close).toHaveBeenCalledWith(3)
      expect(LAYUI.table.reload).toHaveBeenCalled()

      // 改发单价：失败 ⇒ 只弹后端 msg、不关层、不 reload
      mockedRequest.mockReset()
      mockedRequest.mockRejectedValue(new BizError(1000, '发单价不能为空'))
      LAYUI.msg.mockClear()
      LAYUI.layer.close.mockClear()
      LAYUI.table.reload.mockClear()
      await submitThrowPrice('999')
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('发单价不能为空', { time: 1500 }))
      expect(dialogCloses()).toEqual([])
      expect(LAYUI.table.reload).not.toHaveBeenCalled()

      // 收款状态：成功（两步式全合格）⇒ `onDone` 里 close(7) + reload + msg
      mockedRequest.mockReset()
      mockedRequest.mockResolvedValue(BATCH_OK_ONE as never)
      LAYUI.msg.mockClear()
      LAYUI.layer.close.mockClear()
      LAYUI.table.reload.mockClear()
      await submitPaymentStatus()
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('收款状态已更新', { time: 1500 }))
      expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
      expect(LAYUI.table.reload).toHaveBeenCalled()

      // 收款状态：失败 ⇒ 只弹后端 msg、不关层、不 reload
      mockedRequest.mockReset()
      mockedRequest.mockRejectedValue(new BizError(1002, '订单不存在或已被删除'))
      LAYUI.msg.mockClear()
      LAYUI.layer.close.mockClear()
      LAYUI.table.reload.mockClear()
      await submitPaymentStatus()
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('订单不存在或已被删除', { time: 1500 }),
      )
      expect(dialogCloses()).toEqual([])
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      cleanup()
    })
  })

  /* ═══════════ T7：发单价列第三支 → 「查看或编辑订单接手绩效」弹层 ═══════════ */

  /**
   * T7（spec §7.2 / §7.3 / §8 第 27–29 条 / Z7）。
   *
   * 判据层次：
   *  - **三支判据**（判据函数）打在 `order-orderlist-columns.spec.ts`，这里打**点击行为**：
   *    ②支仍是 prompt（**不是**弹层）、③支开弹层；
   *  - 弹层列标题**恰好**七列（顺序也断）；
   *  - 铅笔 / 行内编辑 / 垃圾桶三条写路径的**请求体**（经真 `api/performances.ts`）；
   *  - MF-2 / MF-3：指派弹层账单表那颗垃圾桶的 `title` 是新拟文案，且两颗按钮接上**同一套**动作。
   */
  describe('T7 绩效弹层（发单价列第三支 → 查看或编辑订单接手绩效）', () => {
    /** 造一条「曾抛过、不在大厅、已有人接」的订单行。 */
    const THROWN_ROW = {
      ...ROW,
      hall_publisher_name: 'publisher1',
      status: 'IN_PROGRESS',
      taker_name: '打手一',
    }

    /** `GET /performances/` 替身响应：一条接手绩效行（含本批三个新键）。 */
    const PERF_ITEMS = {
      items: [
        {
          id: 31,
          order_no: ROW.order_no,
          user: 7,
          display_name: '打手一',
          temp_amount: '66.00',
          bonus_amount: '0.00',
          taker_project: '三角洲',
          created_at: '2026-09-23T10:30:00+08:00',
          log_user_name: '录单人甲',
        },
      ],
    }

    /**
     * 用页面真实的 templet 造出「发单价」那一格并点它（与「接手人」`.jsr` 那条用例同形）。
     * 默认行 = ③支（曾抛过 + 不在大厅 + 已有人接）。
     */
    function clickThrowCell(root: HTMLElement, row: typeof ROW = THROWN_ROW): void {
      const cols = buildOrderCols(
        () => 'G',
        () => '',
      )
      const cellEl = document.createElement('div')
      cellEl.innerHTML = (cols.find((c) => c.field === '_throw')!.templet as (d: typeof ROW) => string)(
        row,
      )
      root.appendChild(cellEl)
      cellEl
        .querySelector<HTMLElement>('a.changeThrowFee')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }

    /**
     * 与 `clickThrowCell` 同形，但返回 templet 产出的**第一个**节点（不按 class 取、也**不点**）。
     *
     * ⚠️ 只给 F2 用：它要同时看到**两种 class**（有接手人那支是 `changeThrowFee`、没接手人那支是
     * `changeThrowFeeEdit`），按某一个 class 取会漏掉另一支。既有 `clickThrowCell` 的按 class 取
     * **不动**（它顺带钉住「③支的 class 仍是 `changeThrowFee`」）。
     */
    function attachThrowCellAny(root: HTMLElement, row: Record<string, unknown>): HTMLElement {
      const cols = buildOrderCols(() => 'G', () => '')
      const cellEl = document.createElement('div')
      cellEl.innerHTML = (cols.find((c) => c.field === '_throw')!.templet as (d: Record<string, unknown>) => string)(
        row,
      )
      root.appendChild(cellEl)
      return cellEl.firstElementChild as HTMLElement
    }

    /** 最后一次 `layer.prompt` 的入参元组（不用 `Array.prototype.at`，本工程 lib 早于 ES2022）。 */
    function lastPromptCall(): unknown[] {
      const calls = LAYUI.layer.prompt.mock.calls
      return calls[calls.length - 1]!
    }

    /** 本 describe 用到的三个端点（路径参数 = `PERF_ITEMS` 那一行的 `id`）。 */
    const ADJUST_AMOUNT_URL = '/api/business/performances/31/adjust-amount/'
    const PERF_DELETE_ROW_URL = '/api/business/performances/31/delete/'
    const PERF_LIST_URL = '/api/business/performances/'

    /**
     * 到某个 URL 的请求配置（按调用顺序）。
     *
     * ⚠️ 用「按 URL 筛」而不是 `lastRequestConfig()`：成功之后页面会**再拉一次**列表
     * （A3），最后一个请求因此不一定是那次写请求。
     */
    function callsTo(url: string): { method?: string; url?: string; data?: unknown }[] {
      return mockedRequest.mock.calls
        .map((c) => (c[0] ?? {}) as { method?: string; url?: string; data?: unknown })
        .filter((c) => c.url === url)
    }

    it('③支点击 ⇒ 开的是绩效弹层（标题 / area / anim / shade 逐值取对标）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      const opts = lastOpenOpts()
      expect(opts.type).toBe(1)
      expect(opts.title).toBe('查看或编辑订单接手绩效')
      expect(opts.area).toEqual(['60%', '45%'])
      expect(opts.anim).toBe(8)
      expect(opts.skin).toBe('layui-layer-lan')
      expect(opts.shade).toEqual([0.3, '#333'])
      expect(opts.shadeClose).toBe(true)
      // 读接口被调过（`emp_type=打单工资`）
      expect(mockedRequest).toHaveBeenCalledWith(
        expect.objectContaining({
          method: 'get',
          params: expect.objectContaining({ emp_type: '打单工资' }),
        }),
      )
      cleanup()
    })

    it('②支（大厅里）点击 ⇒ 仍是 prompt，**不是**弹层', async () => {
      const { root, cleanup } = await mountPage()
      clickThrowCell(root, {
        ...ROW,
        hall_publisher_name: 'p',
        status: 'WAITING_GRAB',
        taker_name: '',
      })
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(LAYUI.layer.prompt).toHaveBeenCalled()
      cleanup()
    })

    /**
     * ⭐ **有接手人**的单（哪怕从未抛过大厅、`hall_publisher_name` 为空）
     * 点发单价必须开**绩效弹层**，不是「改发单价」prompt——
     * 判据顺序反了就会让「直接指派、没抛过」的单弹错窗。
     */
    it('F2：有接手人 + 从未抛过大厅 ⇒ 开绩效弹层，**不是**改发单价 prompt', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      const el = attachThrowCellAny(root, {
        ...ROW,
        hall_price: '80.00',
        hall_publisher_name: '',
        status: 'IN_PROGRESS',
        taker_name: '测试打手乙',
      })
      expect(el.className).toBe('changeThrowFee')
      expect(el.dataset.branch).toBe('dialog')
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
      expect(lastOpenOpts().title).toBe('查看或编辑订单接手绩效')
      cleanup()
    })

    /** F2 对照（防一刀切）：**池内**（`UNASSIGNED`）且从未抛过 ⇒ 仍是「改发单价」prompt。 */
    it('F2 对照：池内 + 从未抛过 ⇒ 仍是改发单价 prompt，**不开**绩效弹层', async () => {
      const { root, cleanup } = await mountPage()
      const el = attachThrowCellAny(root, {
        ...ROW,
        hall_price: '80.00',
        hall_publisher_name: '',
        status: 'UNASSIGNED',
        taker_name: '',
      })
      expect(el.className).toBe('changeThrowFeeEdit')
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(LAYUI.layer.prompt).toHaveBeenCalled()
      expect((lastPromptCall()[0] as Record<string, unknown>).title).toBe('更改发单价，可为0，但不能为空')
      expect(LAYUI.open).not.toHaveBeenCalled()
      cleanup()
    })

    /**
     * ⭐ R-BO-1页测试：**无权查看的角色看池内单**——库里没有接单人（D-0032），
     * 但载荷里的 `taker_name` 是常量哨兵 `无权查看`。点发单价必须开**改发单价 prompt**，
     * 不是绩效层；把判据改回 `taker_name !== ''` 这条必红。
     */
    it('R-BO-1：无权查看哨兵 + 池内 ⇒ 开改发单价 prompt，**不是**绩效层', async () => {
      const { root, cleanup } = await mountPage()
      const el = attachThrowCellAny(root, {
        ...ROW,
        hall_price: '80.00',
        hall_publisher_name: 'p',
        status: 'WAITING_GRAB',
        taker_name: '无权查看',
      })
      expect(el.className).toBe('changeThrowFee')
      expect(el.dataset.branch).toBe('prompt')
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(LAYUI.layer.prompt).toHaveBeenCalled()
      expect((lastPromptCall()[0] as Record<string, unknown>).title).toBe(
        '改发单价，可为0，但不能为空，带%号代表按接单价比例发单',
      )
      cleanup()
    })

    it('弹层表的列标题**恰好**是那七列（顺序也断）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      const ths = [...document.querySelectorAll<HTMLElement>('table.tt-perf-rows thead th')]
      expect(ths.map((th) => th.textContent)).toEqual([
        '用户名',
        '预计绩效',
        '生效绩效',
        '接手项目',
        '记录时间',
        '记录人',
        '操作',
      ])
      cleanup()
    })

    it('F2-U1：七列 width 逐值（12%/10%/10%/空/12%/12%/100px）+ align 逐值 + 包裹层（report §3.4 / §4.2）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      const body = String(lastOpenOpts().content)
      runDialogSuccess()
      await flush()
      // 包裹层：form.layui-form + 三边 margin 的 div（证据件页面顶部那两行）
      expect(body).toContain('<form class="layui-form" action="">')
      expect(body).toContain('style="margin-left:10px;margin-top:10px;margin-right:10px"')
      // 列宽断在**渲染出来的** `<col>` 上（不是拿源码常量自指）
      const cols = [...document.querySelectorAll<HTMLElement>('table.tt-perf-rows colgroup col')]
      expect(cols).toHaveLength(7)
      expect(cols.map((col) => col.style.width)).toEqual([
        '12%',
        '10%',
        '10%',
        '',
        '12%',
        '12%',
        '100px',
      ])
      // align 逐值落在每格的 `.layui-table-cell` div 上（report §6.4）
      const headerCells = [
        ...document.querySelectorAll<HTMLElement>('table.tt-perf-rows thead .layui-table-cell'),
      ]
      expect(headerCells).toHaveLength(7)
      expect(headerCells.map((cell) => cell.getAttribute('align'))).toEqual([
        'left',
        'center',
        'center',
        'left',
        'center',
        'center',
        'center',
      ])
      // 列头七项逐字（顺序也断）
      const ths = [...document.querySelectorAll<HTMLElement>('table.tt-perf-rows thead th')]
      expect(ths.map((th) => th.textContent)).toEqual([
        '用户名',
        '预计绩效',
        '生效绩效',
        '接手项目',
        '记录时间',
        '记录人',
        '操作',
      ])
      cleanup()
    })

    it('F2-U1b：三处颜色逐值（用户名 `#006EE9`、两个金额 `#F3723A`、接手项目 `#666`）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      const nameSpan = document.querySelector<HTMLElement>(
        'table.tt-perf-rows tbody td:nth-child(1) span',
      )
      const tempSpan = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="temp_amount"]',
      )
      const bonusSpan = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="bonus_amount"]',
      )
      const projectSpan = document.querySelector<HTMLElement>(
        'table.tt-perf-rows tbody td:nth-child(4) span',
      )
      expect(nameSpan!.getAttribute('style')).toBe('color:#006EE9')
      expect(tempSpan!.getAttribute('style')).toContain('color:#F3723A')
      expect(bonusSpan!.getAttribute('style')).toContain('color:#F3723A')
      expect(projectSpan!.getAttribute('style')).toContain('color:#666')
      cleanup()
    })

    it('F2-U2：金额**原样**显示——`120` / `0`（不补两位小数）', async () => {
      mockedRequest.mockResolvedValue({
        items: [{ ...PERF_ITEMS.items[0], temp_amount: '120', bonus_amount: '0' }],
      } as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      const temp = document.querySelector<HTMLElement>('table.tt-perf-rows [data-cell="temp_amount"]')
      const bonus = document.querySelector<HTMLElement>('table.tt-perf-rows [data-cell="bonus_amount"]')
      expect(temp!.textContent).toBe('120')
      expect(bonus!.textContent).toBe('0')
      expect(
        document.querySelector<HTMLElement>('table.tt-perf-rows tbody')!.textContent,
      ).not.toContain('120.00')
      cleanup()
    })

    it('F2-U3：40px 行高那条规则**只**打到本弹层的表格（同页别的 `.layui-table-cell` 不吃它）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      const inside = document.querySelector<HTMLElement>(
        'table.tt-perf-rows tbody .layui-table-cell',
      )
      expect(inside).not.toBeNull()
      expect(window.getComputedStyle(inside!).height).toBe('40px')
      expect(window.getComputedStyle(inside!).lineHeight).toBe('40px')
      expect(window.getComputedStyle(inside!).fontSize).toBe('12px')
      // ⚠️ 本页证据件**自己**也有一条全局
      // `.layui-table-cell{height:40px;line-height:40px;padding:0 4px;…}` ⇒ 光看行高区分不出两条
      // 规则。写死的差别是那张表的 padding：**本弹层 `0 5px`**（对标子页内联样式）、主表 `0 4px`。
      expect(window.getComputedStyle(inside!).paddingLeft).toBe('5px')
      // 对照：同一个类名、但没有 `tt-perf-rows` 祖先 ⇒ 那条作用域规则命中不了它（吃的仍是主表那条）
      const other = document.createElement('div')
      other.innerHTML =
        '<table><tbody><tr><td><div class="layui-table-cell">x</div></td></tr></tbody></table>'
      document.body.appendChild(other)
      const outside = other.querySelector<HTMLElement>('.layui-table-cell')!
      expect(window.getComputedStyle(outside).paddingLeft).not.toBe('5px')
      cleanup()
    })

    it('F2-U4：两颗按钮的 class / title / 图标 class 逐值（report §5.1）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      const pencil = document.querySelector<HTMLElement>('table.tt-perf-rows [data-act="perf-edit"]')!
      expect(pencil.className).toBe('layui-btn layui-btn-xs layui-btn-warm')
      expect(pencil.getAttribute('title')).toBe('修改该记录的预计绩效金额')
      expect(pencil.getAttribute('style')).toBe('margin-right:0px')
      expect(pencil.querySelector('i')!.className).toBe('layui-icon layui-icon-edit')
      const trash = document.querySelector<HTMLElement>('table.tt-perf-rows [data-act="perf-clear"]')!
      expect(trash.className).toBe('layui-btn layui-btn-xs layui-btn-danger')
      expect(trash.getAttribute('title')).toBe('删除该绩效记录')
      expect(trash.getAttribute('style')).toBe('margin-right:0px')
      expect(trash.querySelector('i')!.className).toBe('layui-icon layui-icon-delete')
      cleanup()
    })

    it('「记录时间」显示成 `MM-DD HH:mm`（对标 `createTime` 的 11 字符形状，report §7）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      const cells = [...document.querySelectorAll<HTMLElement>('table.tt-perf-rows tbody tr td')]
      expect(cells).toHaveLength(7)
      expect(cells[4]!.textContent).toBe('09-23 10:30')
      cleanup()
    })

    it('U1：点「预计绩效」格 ⇒ 格内出 `<input>`、不出 `layer.prompt`；提交后 body 恰好两个键', async () => {
      // ⭐ **第二次**列表响应回的 temp 与用户输入**不同**（88.00 进、87.99 出）
      // ⇒ 断言格内显示的是**服务端值 87.99**。这条把「本地假装等于 V、又顺手重拉」的假实现杀掉：
      // 只重拉不采用、或本地写成 88.00，都会红。
      const PERF_ITEMS_AFTER = {
        items: [{ ...PERF_ITEMS.items[0], temp_amount: '87.99' }],
      }
      // 按 URL 分派而不是按调用顺序：写成功后的 `refreshPerfViews()` 与 `reload()` 谁先到
      // 不是这条判据要钉的东西（顺序若变不该让它红）。
      let wrote = false
      mockedRequest.mockReset()
      mockedRequest.mockImplementation(((config: { url?: string }) => {
        if (config.url === ADJUST_AMOUNT_URL) {
          wrote = true
          return Promise.resolve(PERF_ITEMS)
        }
        if (config.url === PERF_LIST_URL) {
          return Promise.resolve(wrote ? PERF_ITEMS_AFTER : PERF_ITEMS)
        }
        return Promise.resolve(PERF_ITEMS)
      }) as never)

      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      LAYUI.layer.prompt.mockClear()
      // ⚠️ `mockClear()` 只清调用记录、不清实现 ⇒ 上面的 URL 分派仍在（`wrote` 仍是 false）。
      mockedRequest.mockClear()

      const cell = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="temp_amount"]',
      )!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      // 行内编辑：**不弹二级窗**（spec §1-S1/S2）
      expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
      const input = cell.querySelector<HTMLInputElement>('input')
      expect(input).not.toBeNull()
      expect(input!.getAttribute('type')).toBe('text')
      expect(input!.getAttribute('inputmode')).toBe('decimal')
      expect(input!.value).toBe('66.00')

      input!.value = '88.00'
      input!.dispatchEvent(new FocusEvent('blur'))
      await vi.waitFor(() => expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1))
      expect(callsTo(ADJUST_AMOUNT_URL)[0]).toEqual({
        method: 'post',
        url: ADJUST_AMOUNT_URL,
        data: { amount_kind: 'temp', amount: '88.00' },
      })
      // A3：成功后**再拉一次**绩效列表（开层那次已被 `mockClear` 清掉）
      await vi.waitFor(() => expect(callsTo(PERF_LIST_URL)).toHaveLength(1))
      // Minor-4：格内显示服务端回的 `87.99`（**不是**用户填的 `88.00`）；属性与正文两处都断。
      await vi.waitFor(() => {
        const after = document.querySelector<HTMLElement>(
          'table.tt-perf-rows [data-cell="temp_amount"]',
        )!
        expect(after.textContent).toBe('87.99')
        expect(after.dataset.fee).toBe('87.99')
      })
      cleanup()
    })

    it('U1b：提交进行中再点同一格 ⇒ 不重开输入框、也不发第二次请求（防重复提交）', async () => {
      let releasePost: (() => void) | null = null
      // mount 会取一次 `/api/auth/me` ⇒ 先给一个确定响应。
      mockedRequest.mockReset()
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)

      const { root, cleanup } = await mountPage()
      // ⚠️ 本用例要的「**一次挂起**」序列必须装在 mount **之后**：那条 `mockResolvedValueOnce`
      //    会被 mount 的 me 那一发吃掉，后面整条顺序错位（实测：绩效格根本不渲染）。
      mockedRequest.mockReset()
      mockedRequest
        .mockResolvedValueOnce(PERF_ITEMS as never)
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              releasePost = () => resolve(PERF_ITEMS as never)
            }),
        )
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)

      clickThrowCell(root)
      runDialogSuccess()
      await flush()

      const cell = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="temp_amount"]',
      )!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const input = cell.querySelector<HTMLInputElement>('input')!
      input.value = '88.00'
      input.dispatchEvent(new FocusEvent('blur'))
      await vi.waitFor(() => expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1))

      // 请求还没回来 ⇒ 再点不重开（还是同一个输入节点、且被禁用）、也不重复提交
      const before = cell.querySelector<HTMLInputElement>('input')!
      expect(before.disabled).toBe(true)
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(cell.querySelector('input')).toBe(before)
      expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1)

      releasePost!()
      await flush()
      cleanup()
    })

    it('Minor-3①：行内编辑按 `Esc` ⇒ 取消（格内回到原值、一个请求都不发）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()

      const cell = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="temp_amount"]',
      )!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const input = cell.querySelector<HTMLInputElement>('input')!
      input.value = '99.00'
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
      // ⛔ `Esc` 是**取消**：`commit()` 都不该被调到，故一次 `request()` 都没有
      expect(mockedRequest).not.toHaveBeenCalled()
      expect(cell.querySelector('input')).toBeNull()
      expect(cell.textContent).toBe('66.00')
      cleanup()
    })

    it('Minor-3②：行内「与之前一样」⇒ 不发请求（只把格子还原）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()
      LAYUI.msg.mockClear()

      const cell = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="temp_amount"]',
      )!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const input = cell.querySelector<HTMLInputElement>('input')!
      // 预填值原样不动（= 与之前一样）
      expect(input.value).toBe('66.00')
      input.dispatchEvent(new FocusEvent('blur'))
      await flush()
      expect(mockedRequest).not.toHaveBeenCalled()
      expect(cell.querySelector('input')).toBeNull()
      expect(cell.textContent).toBe('66.00')
      // 行内这一支对标自己 `ReferenceError` ⇒ 不抄那条「与之前一样，无需修改」（spec §4.7）：
      // 只还原、不弹 msg（铅笔那条仍弹，见 U3 同族判据）。
      expect(LAYUI.msg).not.toHaveBeenCalled()
      cleanup()
    })

    it('Minor-3③：行内非法金额 ⇒ 提示逐字、不发请求、格子还原', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()
      LAYUI.msg.mockClear()

      const cell = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="temp_amount"]',
      )!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const input = cell.querySelector<HTMLInputElement>('input')!
      input.value = 'abc'
      input.dispatchEvent(new FocusEvent('blur'))
      await flush()
      expect(mockedRequest).not.toHaveBeenCalled()
      // 文案**逐字**取对标 `VerifyMoeny` 失败支，`<原值>` = 预填值（spec §4.7）
      expect(LAYUI.msg.mock.calls.map((c) => c[0])).toContain('请输入正确的金额，默认为66.00')
      expect(cell.querySelector('input')).toBeNull()
      expect(cell.textContent).toBe('66.00')
      cleanup()
    })

    it('U3：铅笔 ⇒ **仍走** `layer.prompt`（title/anim/value 逐值不变）；确认后同样走 `adjust-amount`', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()

      document
        .querySelector<HTMLElement>('table.tt-perf-rows [data-act="perf-edit"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const promptCall = lastPromptCall()
      const promptOpts = promptCall[0] as Record<string, unknown>
      expect(promptOpts.title).toBe('修改预计/生效绩效')
      expect(promptOpts.value).toBe('66.00')
      expect(promptOpts.anim).toBe(2)
      const cb = promptCall[1] as (v: string, i: number) => void
      cb('88.00', 9)
      await vi.waitFor(() => expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1))
      expect(callsTo(ADJUST_AMOUNT_URL)[0]).toEqual({
        method: 'post',
        url: ADJUST_AMOUNT_URL,
        data: { amount_kind: 'temp', amount: '88.00' },
      })
      cleanup()
    })

    /**
     * ⭐ D1 / R9 #11a（spec §7.1 第 1 条）：绩效铅笔那条 `layer.prompt` **成功才关层**。
     *
     * ⚠️ 层号用**用例注入的那个 9**：`toHaveBeenCalled()` 是恒真判据，抓不到「关错层」；
     * 而这条实现只可能关它自己收到的那一个 index（见 `promptPerformanceAmount`）。
     */
    it('D1-#11a 成功 ⇒ 关**它自己那层**（用例注入 9），失败 ⇒ 不关层', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()
      LAYUI.msg.mockClear()
      LAYUI.layer.close.mockClear()

      document
        .querySelector<HTMLElement>('table.tt-perf-rows [data-act="perf-edit"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      ;(lastPromptCall()[1] as (v: string, i: number) => void)('88.00', 9)
      await vi.waitFor(() => expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1))
      await vi.waitFor(() => expect(LAYUI.layer.close).toHaveBeenCalledWith(9))
      cleanup()
    })

    /** ⭐ D1 / R9 #11b（spec §7.1 第 2 条）：失败**留层**，用户能在原框重试。 */
    it('D1-#11b 失败 ⇒ **不关层**（用户能在原框重试）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()
      LAYUI.msg.mockClear()
      LAYUI.layer.close.mockClear()

      document
        .querySelector<HTMLElement>('table.tt-perf-rows [data-act="perf-edit"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      mockedRequest.mockRejectedValue(
        new BizError(1014, '生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改'),
      )
      ;(lastPromptCall()[1] as (v: string, i: number) => void)('88.00', 9)
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改',
          { time: 1500 },
        ),
      )
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })

    /**
     * ⭐ D1 / R9 #11c（spec §7.1 第 4 条）：**行内编辑**那条路不许关任何层。
     *
     * 两条路共用一个出口 `submitPerformanceAmount`（成功返回 `true`）——`layer.close` 只能加在
     * 铅笔回调里：行内编辑那条路上**没有层、也没有 index**。
     */
    it('D1-#11c 回归钉：**行内编辑**那条路不许关任何层（⛔ 别把 layer.close 加进 submitPerformanceAmount）', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      mockedRequest.mockClear()
      LAYUI.layer.close.mockClear()

      const cell = document.querySelector<HTMLElement>('table.tt-perf-rows [data-cell="temp_amount"]')!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const input = cell.querySelector<HTMLInputElement>('input')!
      input.value = '88.00'
      input.dispatchEvent(new FocusEvent('blur'))
      await vi.waitFor(() => expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1))
      // ⚠️ 必须等**成功支跑完**再断言「没关层」：`LAYUI.layer.close` 若被误加进
      //    `submitPerformanceAmount` 的成功支，它发生在那一发请求的 `.then` 里——只等
      //    「请求已发出」会在微任务之前就断言，变异会**存活**（实测过一次）。
      await vi.waitFor(() => expect(callsTo(PERF_LIST_URL)).toHaveLength(1))
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })

    it('U2：点「生效绩效」格 ⇒ 同样出 `<input>`；`Enter` 提交 ⇒ `amount_kind="bonus"`', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()
      LAYUI.layer.prompt.mockClear()
      mockedRequest.mockClear()

      const cell = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-cell="bonus_amount"]',
      )!
      cell.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(LAYUI.layer.prompt).not.toHaveBeenCalled()
      const input = cell.querySelector<HTMLInputElement>('input')!
      expect(input.value).toBe('0.00')

      input.value = '5.00'
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await vi.waitFor(() => expect(callsTo(ADJUST_AMOUNT_URL)).toHaveLength(1))
      expect(callsTo(ADJUST_AMOUNT_URL)[0]).toEqual({
        method: 'post',
        url: ADJUST_AMOUNT_URL,
        data: { amount_kind: 'bonus', amount: '5.00' },
      })
      cleanup()
    })

    it('U4：垃圾桶 ⇒ 文案逐字取对标；确认后调 delete 端点、成功后再拉一次列表并提示', async () => {
      mockedRequest.mockResolvedValue(PERF_ITEMS as never)
      const { root, cleanup } = await mountPage()
      clickThrowCell(root)
      runDialogSuccess()
      await flush()

      const trash = document.querySelector<HTMLElement>(
        'table.tt-perf-rows [data-act="perf-clear"]',
      )!
      expect(trash.getAttribute('title')).toBe('删除该绩效记录')
      // 值纪律不动：`lay-event` / class / 图标仍是 live 原文（Z7）
      expect(trash.getAttribute('lay-event')).toBe('delEmp')
      expect(trash.className).toContain('layui-btn layui-btn-xs layui-btn-danger')
      expect(trash.querySelector('i')!.className).toContain('layui-icon-delete')
      trash.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const confirmCalls = LAYUI.confirm.mock.calls
      const confirmCall = confirmCalls[confirmCalls.length - 1]!
      expect(confirmCall[0]).toBe(
        '确认将该笔绩效删除吗？删除后可能会影响订单当前接手人',
      )
      mockedRequest.mockClear()
      LAYUI.msg.mockClear()
      ;(confirmCall[1] as (i: number) => void)(4)
      await vi.waitFor(() => expect(callsTo(PERF_DELETE_ROW_URL)).toHaveLength(1))
      expect(callsTo(PERF_DELETE_ROW_URL)[0]).toEqual({
        method: 'post',
        url: PERF_DELETE_ROW_URL,
      })
      // ⭐ R9 #18（spec §3.1 对照表追加行）：垃圾桶那层是**先关后发** ⇒ 确认即关它自己那层（4）、
      // 恰好一次；层号用**用例注入的 4**，⛔ 不用恒真的 `toHaveBeenCalled()`。
      expect(LAYUI.layer.close).toHaveBeenCalledTimes(1)
      expect(LAYUI.layer.close).toHaveBeenCalledWith(4)
      // A3 同款：删除成功后**再拉一次**列表
      await vi.waitFor(() => expect(callsTo(PERF_LIST_URL)).toHaveLength(1))
      // `msg` 的第二参是 layui 的选项对象 ⇒ 只判第一参（提示语）
      expect(LAYUI.msg.mock.calls.map((c) => c[0])).toContain('已删除该绩效记录')
      cleanup()
    })

    it('MF-2 / MF-3：指派弹层账单表与绩效弹层的两颗按钮走同一套动作（软删 + 铅笔 prompt）', async () => {
      mockRequests(PERF_ITEMS as never)
      LAYUI.open.mockImplementation(() => 7)
      LAYUI.table.checkStatus.mockReturnValue({ data: [{ ...ROW }] })
      const { root, cleanup } = await mountPage()
      clickToolbar(root)
      runDialogSuccess()
      await flush()

      const bill = document.querySelector<HTMLElement>('table.tt-assign-bills')!
      const trash = bill.querySelector<HTMLElement>('[data-act="perf-clear"]')!
      expect(trash.getAttribute('title')).toBe('删除该绩效记录')
      // MF-3：铅笔没反应 → 现在开 prompt 且预填该行 temp_amount
      const pencil = bill.querySelector<HTMLElement>('[data-act="perf-edit"]')!
      pencil.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const promptCall = lastPromptCall()
      expect((promptCall[0] as Record<string, unknown>).title).toBe('修改预计/生效绩效')
      expect((promptCall[0] as Record<string, unknown>).value).toBe('66.00')
      // MF-3：垃圾桶走同一句 confirm + 同一个软删端点
      trash.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const confirmCalls = LAYUI.confirm.mock.calls
      const confirmCall = confirmCalls[confirmCalls.length - 1]!
      expect(confirmCall[0]).toBe(
        '确认将该笔绩效删除吗？删除后可能会影响订单当前接手人',
      )
      mockedRequest.mockClear()
      ;(confirmCall[1] as (i: number) => void)(4)
      await vi.waitFor(() => expect(callsTo(PERF_DELETE_ROW_URL)).toHaveLength(1))
      expect(callsTo(PERF_DELETE_ROW_URL)[0]).toEqual({
        method: 'post',
        url: PERF_DELETE_ROW_URL,
      })
      cleanup()
    })
  })
})
