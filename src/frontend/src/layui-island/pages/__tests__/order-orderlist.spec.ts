// @vitest-environment jsdom
import { renderOrderRows } from './order-list-table-fixture'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildFilterSearchConfig,
  buildFilterSearchData,
  buildOrderCols,
  defaultDateRange,
  emptyStateText,
  hasActiveFilters,
  fetchPushAvailability,
  FILTER_OPTIONS_SEARCH_URL,
  isPushAvailabilityAvailable,
  LOSS_PRICE_RANGE_IGNORED_TEXT,
  newRefundRequestId,
  NOT_OPEN_FILTERS,
  NOT_OPEN_TOOLBAR,
  ORDER_NO_DEFAULT_COLOR,
  ORDER_NO_PUSHED_COLOR,
  PAGE_LIMITS,
  platformChildCellHtml,
  PLATFORM_CHILD_EDIT_CLASS,
  PLATFORM_CHILD_EMPTY_VALUE,
  PLATFORM_CHILD_INDENT_PX,
  PLATFORM_CHILD_ROW_CLASS,
  PLATFORM_CHILDREN_STYLE_RULES,
  PLATFORM_TREE_SELECTOR,
  popgameRowsFromEnvelope,
  POPGAME_PLACEHOLDER,
  publisherRowsFromEnvelope,
  // 三颗 + 未接手时的撤回箭头（加款/重发/协商撤销已搬到第三方页）。
  PUSH_JDP_CANCEL_CLASS,
  PUSH_JDP_CANCEL_CONFIRM_TEXT,
  PUSH_JDP_CLASS,
  PLATFORM_STATUS_NOTICE,
  PUSH_JDP_NO_PLATFORM_MESSAGE,
  pushRowAction,
  PUSH_AVAILABILITY_URL,
  PUSH_JDP_GATE_TIMEOUT_MS,
  pushJdpLayerHeight,
  REFUND_DIALOG_TITLE,
  REFUND_INVALID_AMOUNT_MESSAGE,
  REFUND_SUCCESS_MESSAGE,
  renderPopgameName,
  setOrderListPermissions,
  setPushJdpAvailable,
  STATUS_COLORS,
  statusColor,
  statusLabel,
  THROW_DIALOG_TITLE,
  toIsoDateTime,
  toOrderSearch,
  usedNotOpenFilters,
  WITHDRAW_CONFIRM_TEXT,
} from '../order-orderlist'
// ⭐ 平台写动作（发单地球 / 撤回箭头）的节点门。
import {
  applyPlatformWritePermission,
  resetPlatformWriteAllowedForTests,
  setPlatformWriteNodeShipped,
} from '@/layui-island/perms'
// `@/api/orders` 在本文件顶部被替身化（`vi.mock`），这里 import 到的就是那几个 vi.fn，
// 用 `vi.mocked(...)` 拿回类型（`vi.mock` 的工厂返回值不会被 TS 当成 mock）。
import {
  orderPlatformPushUrl,
  type DailiantongPushRow,
  type PlatformPushSummary,
} from '@/api/dailiantongPush'
import { fetchOrder, transitionOrder } from '@/api/orders'
import { BizError, AuthExpiredError, HttpError } from '@/api/errors'
// 进入编辑态的上报出口（本文件顶部被替身化）：用来断言**触发点**是「打开编辑弹窗」那一刻。
import { reportOrderOpenEditQuietly } from '@/api/auditLogs'
import { throwToHall, withdrawThrow } from '@/api/hall'
// ⚠️ **传输层替身**：`@/api/orders` 的 `partialRefund` **保持真身**（只替它下面那一层
// `request()`），这样「记退款的 URL 与 body」断言的是**真的** `api/orders.ts` 拼出来的
// 请求配置（T2），而不是某个替身的入参形状。
import { request } from '@/api/http'
// 「编辑保存成功」两端的**同一份**定义（发送方在录单页）。**故意从发送方 import**：
// 各写一份字面量时，改名会让两边的测试各自都绿、合起来却不工作（本项目最忌讳的那种分叉）。
import { EDIT_SAVE_SUCCESS_MESSAGE, ORDER_SAVED_MESSAGE_TYPE } from '../order-addorder'
import { PUSH_JDP_SENT_MESSAGE_TYPE, PUSH_JDP_SENT_TEXT } from '@/layui-island/platformPush'

const HERE = path.dirname(fileURLToPath(import.meta.url))

/**
 * ⭐ **复刻偏离登记（§5-D，业务方 2026-11-09 拍板）**：本页「代练打手 / 发单人」两个下拉
 * （`#popgameName` / `#hallPublisherUser`）——**对标 = 全量 option + 本地搜索；
 * 我方 = 关键词远程搜索**（U14 截断的延伸修正：最大在役租户 729 人底集预载截断 200，租户名不进 src、判据见 docs/plans/2026-11-09-orderlist-taker-search.md，
 * `sq1551` 位次 339 搜不到人）。判据分两层：本文件钉结构（`xm-select-search` 属性值 =
 * 端点 URL）与纯函数（信封拆包 / formselects ajax 配置怎么造）；页面级行为
 * （挂载 / 切游戏零 `filter-options` 请求、切档切游戏重置为仅占位 option、
 * `formSelects.config` 注入）在 `order-orderlist-filters.spec.ts`。
 */

/** 证据件原文（`public/replica/order-orderlist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-orderlist.html'),
  'utf8',
)

/**
 * 「订单状态」筛选下拉整段（`select[name=orderSta]`）的**原文**。守卫测试只认这一段里的
 * 可见 `<option>`——同一份证据件里那个隐藏弹窗还有一处「待上号」（`input[name=changeToDSH]`，
 * L486），不把整份文件当判据就不会被它影响。
 */
const ORDER_STA_SELECT = /<select name="orderSta"[\s\S]*?<\/select>/.exec(EVIDENCE)?.[0] ?? ''

/**
 * layui 的**可控替身**（挂载整页用）。`bootstrapIsland` 在 jsdom 里加载不了真的 layui，
 * 所以把它换成这份替身之后，`layer.open` / `layer.msg` 的**入参**才能被断言。
 */
const LAYUI = vi.hoisted(() => {
  const open = vi.fn()
  const msg = vi.fn()
  /**
   * `table.on(...)` / `form.on(...)` 登记的事件回调。
   *
   * ⚠️ **不要**再用「手动触发 handler」来模拟用户翻页：本页曾登记 `table.on('page(datatable)')`
   * 并在用例里手调它，而那个事件在 vendored layui 2.13.9 里**不存在**（E-0037）——手调 handler
   * 等于自己伪造事件源，是假绿。页码的真出口是 `done`（见 `triggerDone`）。
   * `tableHandlers` 现在只用于**负向**判据：本页不应再登记任何 `page(...)` 事件。
   */
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    open,
    msg,
    tableHandlers,
    formHandlers,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
      val: vi.fn(() => ({})),
    },
    table: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
      reload: vi.fn(),
      // 返回类型显式写成 `{ data: unknown[] }`：否则推断成 `never[]`，用例里的
      // `mockReturnValue({ data: [ROW] })` 过不了类型检查。
      checkStatus: vi.fn((): { data: unknown[] } => ({ data: [] })),
      // `table.getOptions(id)` 回的是 `table.render` 时那份**配置对象**（vendored layui 2.13.9：
      // `F.getOptions=l`，而 `l` 返回 `H.config[id]`）。用户选的「条/页」由 laypage **就地**写在
      // 同一个 `page` 子对象上 ⇒ `page.limit` 是实时值。默认 20，用例按需覆写（R2）。
      getOptions: vi.fn((): { page: { limit: number } } => ({ page: { limit: 20 } })),
    },
    layer: {
      open,
      msg,
      close: vi.fn(),
      prompt: vi.fn(),
      tips: vi.fn(),
      confirm: vi.fn(),
      // 选中「已完成」而当前不是待验收时，那扇提示窗走的就是 `layer.alert`
      // （内容是逐字文案；见 `openStatusDialog` 与 U5）。
      alert: vi.fn(),
    },
    laydate: { render: vi.fn() },
    // `layer.open({type:1, content: layui.$(node)})` 要一个 jQuery 形状的对象；真 layui 有 `$`，
    // 替身里必须补上，否则 `openThrowDialog` 在开弹层前就 TypeError。
    //
    // ⚠️ **必须是 `vi.fn`，返回类型写 `unknown`**：B5 那条「content 不许是裸 DOM 节点」的用例
    // 要靠它把返回值换成一个**与节点不同**的替身来分辨（默认仍然直接回节点，所以既有用例拿到的
    // `content` 还是那个元素本身，一字不改）。
    $: vi.fn((el: Element): unknown => el),
    element: { init: vi.fn() },
  }
})

/**
 * 触发最近一次 `table.render` 的 `done(res, curr, count)` —— layui 交回页码的**真实出口**
 * （vendored `public/layui/layui.js` L1 压缩单行、字符偏移 292034 调的是
 * `o.done(e, i, e[r.countName], t)`，**每次取数后**都会调它：首帧 / 翻页 / 改「条/页」都算）。
 * jsdom 里跑不了真 layui，翻页只能这样驱动——但驱动的是真出口，不是手调一个本版本不存在的事件
 * handler（那正是 E-0037 的假绿形态）。
 */
function triggerDone(res: unknown, curr: number, count: number): void {
  const calls = LAYUI.table.render.mock.calls
  const opts = calls[calls.length - 1]?.[0] as
    | { done?: (r: unknown, c?: number, n?: number) => void }
    | undefined
  expect(opts?.done).toBeTypeOf('function')
  opts!.done!(res, curr, count)
}

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

vi.mock('@/api/config', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/config')>()
  return { ...actual, fetchGames: vi.fn(async () => []), fetchServers: vi.fn(async () => []) }
})

vi.mock('@/api/hall', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hall')>()
  return { ...actual, throwToHall: vi.fn(), withdrawThrow: vi.fn() }
})

vi.mock('@/api/orders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/orders')>()
  return {
    ...actual,
    fetchOrder: vi.fn(),
    changePrice: vi.fn(),
    transitionOrder: vi.fn(),
    // ⚠️ 2026-11-09 起本页 mount **不再**取候选名单（去预选，改关键词远程搜索，
    // 见「已知差异 68」与 `order-orderlist-filters.spec.ts`）；这个替身保留是防御性的：
    // 页面若回归成预载， filters spec 的「零请求」判据会红，本文件不受影响。
    fetchFilterOptions: vi.fn(async () => ({
      takers: [],
      publishers: [],
      takers_truncated: false,
      publishers_truncated: false,
    })),
  }
})

// 「订单来源」的候选（本页 mount 时会取一次）——同上，替身化以免污染 `request()` 计数。
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

// 进入编辑态时的读操作上报（spec §6.2）：替身化是为了断言「点编辑确实上报了」，
// 同时避免 jsdom 里对 `/api/audit/events/` 发真请求（那会让本文件的挂载用例带上网络噪声）。
vi.mock('@/api/auditLogs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auditLogs')>()
  return { ...actual, reportOrderOpenEditQuietly: vi.fn() }
})

/**
 * 传输层替身：`request()` 被换掉，**`partialRefund` 保持真身**——「记退款」的
 * URL/data 因此断言在真 `api/orders.ts` 的产物上（见文件顶部 import 处的说明）。
 * 其余函数用 `...actual` 保留（本文件不碰它们在真传输层上的行为）。
 */
vi.mock('@/api/http', () => ({ request: vi.fn(), http: { get: vi.fn() } }))

/**
 * 本页行内入口的**权限位默认值**。`mount` 会取一次 `/api/auth/me` 覆盖它 ⇒ 下面的 `request`
 * 桩给一个「各门都有节点」的身份，让挂载级用例与**加门之前逐字一致**；直调 `buildOrderCols`
 * 的那些用例不走 mount，靠这里的 `setOrderListPermissions` 显式授权（权限门本身的用例在
 * `order-orderlist-phase1.spec.ts`）。
 *
 * ⚠️ 编辑订单的门 = `GET /orders/<no>/` 的 any(`view_detail`, `view_content`)，⛔ 不是
 * `add_edit`；发大厅独占 `order.send_to_grab_hall`（20 只管改发单价）；工具栏「导出」单点
 * `order.export`——少给任何一个，本文件里那些「点末列操作按钮」的既有用例会看到**空入口**
 * （那是门的副作用，不是回归）。
 */
const ORDER_NODES_FOR_TESTS = {
  is_tenant_admin: false,
  is_account_admin: false,
  menu_permissions: [
    // ⭐ 平台写入口（发单地球 / 撤回箭头）挂 `order.platform_write`；后端节点未上线时
    // 前端按 `order.list` 降级（见 `@/layui-island/perms::canPlatformWrite`）⇒ 本夹具给这个节点。
    'order.list',
    'order.add_edit',
    'order.assign_taker',
    // 编辑订单的门（弹层第一发 `GET /orders/<no>/`）。
    'order.view_detail',
    'order.view_screenshot',
    'order.upload_screenshot',
    // 发大厅 / 撤回两颗写入口的门（发大厅 = 36 独占闸）。
    'order.send_to_grab_hall',
    'order.withdraw_from_grab_hall',
    // 工具栏「导出」的门（后端 `OrderExportView` 只认这一个节点）。
    'order.export',
  ],
}

/**
 * `LAYUI.table.getOptions` 是**跨用例共享**的 `vi.fn`：`mockClear()` 不还原 `mockReturnValue`，
 * 所以每个用例开始前把它还原成默认的 20（要 50 / 别的值的用例在自己体内再覆写）。
 */
beforeEach(() => {
  LAYUI.table.getOptions.mockReturnValue({ page: { limit: 20 } })
  // ⭐ 平台写动作的权限门是**模块级状态**（`@/layui-island/perms`）⇒ 每个用例收口
  // （降级档 = 升级前行为；身份取不到照画 —— 与 mount 期 `applyPlatformWritePermission` 同源）。
  setPlatformWriteNodeShipped(false)
  resetPlatformWriteAllowedForTests()
  setOrderListPermissions(ORDER_NODES_FOR_TESTS)
  // 地球按钮（`.pushJdpEdit`）的数据门：直调 `buildOrderCols` 的用例
  // 不经过 mount 那一发 → 默认置 true（不缩既有断言的可见集）；门本身的
  // 「有/无启用账号」判据在文末 describe 里挂载真页、走 `http.get` 桩来驱动。
  setPushJdpAvailable(true)
  // `mount` 里的 `fetchMe()` 走被替身化的 `request`：`/api/auth/me` 回上面那个身份，
  // 其余请求回 `{}`（本文件不断言页面的列表取数——`table.render` 是替身）。
  vi.mocked(request).mockImplementation((async (config: { url?: string }) =>
    config?.url === '/api/auth/me' ? { user: ORDER_NODES_FOR_TESTS } : {}) as never)
})

/**
 * 【订单管理中心】复刻页（`/Order/orderlist`）的**列声明 / 检索参数 / 空态文案**逐条防漂移测试。
 * 断言的是**我们的**列集合（对标 28 列 − 删 4 − 去重 1 = 23 列，列设计见
 * `docs/specs/2026-09-23-order-list-columns-design.md` §2/§7），不是对标的原文数组；
 * 「重要备注」列标题空白是对标 i18n 缺 key 的缺陷，我们**不复刻**（见模块头「已知差异 5」）。
 *
 * 为什么值得写（`parity.mjs` 量不到的三处死角）：
 *  1. 第 1 列是 `type:'checkbox'`（空文案）——漏了它表头勾选框**静默消失**，而探针的表头比对
 *     只读「文案 + 宽度」，空文案的特殊列丢了**不报**；
 *  2. `hide` / `align` / `templet` 这些**空表下不产生可见差异**的声明（列宽与隐藏列的取舍），
 *     一旦被改坏，探针在真实数据上才可能报，而那时已经晚了一步；
 *  3. `page.limits` 是**代码常量**（判 struct），但「共 1 条」时那种位移类差异会被判成 data。
 */
describe('order-orderlist 的 23 列与（删 4 · 去重 1 后的）列集合逐条对齐', () => {
  const cols = buildOrderCols(
    (id) => `G${id}`,
    (id) => (id === null ? '' : `S${id}`),
  )
  const titles = cols.map((c) => c.title)

  it('23 列，顺序是 28 列删掉 4 列、去掉重复「重要备注」后的相对位置（不重排）', () => {
    expect(cols).toHaveLength(23)
    expect(titles).toEqual([
      undefined, // 1 checkbox
      '订单号',
      '系统内置单号',
      '角色名',
      '代练/陪玩内容',
      '角色信息',
      '状态',
      '时间',
      '旺旺/来源',
      '订单来源',
      '接手人',
      '发单价',
      '接单价',
      '实收款',
      '退款',
      '订单截图',
      '销售客服',
      '发单人',
      '录单时间',
      '完单时间',
      '录单人',
      '重要备注',
      '操作',
    ])
  })

  it('删掉的 4 个列标题不再出现（订单标签 / 阵营 / 机位·团长 / 日志）', () => {
    expect(titles).not.toContain('订单标签')
    expect(titles).not.toContain('阵营')
    expect(titles).not.toContain('机位/团长')
    expect(titles).not.toContain('日志')
  })

  it('「重要备注」**恰好一个**（对标成对两列 `_remark2`/`_remark` 的去重，spec 裁定 2）', () => {
    expect(titles.filter((t) => t === '重要备注')).toHaveLength(1)
    expect(cols.filter((c) => c.title === '重要备注')[0]).toMatchObject({ field: '_remark' })
  })

  it('第 1 列是 checkbox（空文案的特殊列，漏了探针不报）', () => {
    expect(cols[0]).toMatchObject({ type: 'checkbox', field: '_orderId0', width: 35 })
  })

  it('列宽**原样取桌面分支**：百分比是字符串，不换算像素（parity 实测 authored 宽度）', () => {
    // 各列宽照对标桌面分支逐条取值（百分比是字符串、不换算像素；「角色信息」桌面分支是
    // 空串 = 自动列）。
    // ⚠️（D-0147）对标实测把**发单价 / 接单价**两列宽取 70 ⇒ 本数组对应两项是 70；「实收款」仍是 78。
    expect(cols.map((c) => c.width)).toEqual([
      35,
      '12%',
      142,
      '5.5%',
      '10.4%',
      undefined, // 角色信息：桌面分支是空串（自动列）
      60,
      80,
      '5%',
      100,
      '4.7%',
      70, // 发单价（对标实测 70）
      70, // 接单价（同上）
      78, // 实收款：仍是 78
      60,
      64,
      60,
      60,
      // ⚠️ 有意的可见偏离：录单/完单时间是定长 16 字形（YYYY-MM-DD HH:mm），对标 75
      // 放不下会截断 ⇒ 放宽到 **150**（业务方指示「时间列固定宽度、别展示不全」）。
      150,
      150,
      60,
      80,
      // ⚠️ 对标写的是 130，但本页有推送行时那一格有 **7** 颗按钮（7 × 28px = 196px）
      // ⇒ 130 放不下（真浏览器实测：第 5 颗起被裁 / 被翻页栏盖住）⇒ 200 = 有意的可见偏离；
      // 最后一颗由图标换成 4 字文字按钮「协商撤销」（≈58px，比图标多 ~30px）⇒ 再放宽到 **240**。
      240,
    ])
  })

  it('隐藏列只剩 2 个：`orderId`（系统内置单号）与 `_completeTime`（完单时间）', () => {
    // 隐藏列就是对标自带的那两列（`orderId` / `_completeTime`），其余各列都默认显示。
    const hidden = cols.filter((c) => c.hide === true).map((c) => c.field)
    expect(hidden).toEqual(['orderId', '_completeTime'])
  })

  it('5 列默认显示：`hide` 逐列都是**假值**（不是「恰好集合」放过去）', () => {
    // 写法与「订单来源」那条一致：**不写 `hide`**（对标那几条表达式恒为 false，
    // 写 `hide:false` 与不写等价）⇒ 判据是 `'hide' in col === false`。
    for (const field of [
      '_netreceipts',
      '_refund',
      '_assUserName',
      'addOrderUserName',
      '_remark',
    ]) {
      const col = cols.find((c) => c.field === field)
      expect(col, `列定义里找不到 field=${field}`).toBeDefined()
      expect('hide' in (col as object), `${field} 仍是隐藏列`).toBe(false)
    }
  })

  it('订单来源仍默认可见（字段名是后端派生的 `source_name`，不写 `hide`）', () => {
    // ⭐ 这一列读后端派生的
    // `source_name`（挂字典 ⇒ 字典当前名；没挂字典 ⇒ 后端回落到名字快照 `source`）。
    // ⚠️ 判据仍是**恰好** `field` 相等（⛔ 不放宽成 `toContain`）：字段名写错 ⇒ 列恒空。
    const col = cols.find((c) => c.title === '订单来源')
    expect(col).toMatchObject({ field: 'source_name' })
    expect('hide' in (col as object)).toBe(false)
  })

  it('末列「操作」写 fixed:right（对标 L2217 原文；宽度 240 是 B4 实测 + G2 文字按钮推导）', () => {
    expect(cols[22]).toMatchObject({ title: '操作', width: 240, fixed: 'right' })
  })

  it('align:center 的 15 列落在删列后的下标上（删掉 `_orderLabel`/`_group` 两列之后）', () => {
    // 对标带 `align:'center'` 的 17 列里，`_orderLabel`/`_group` 已删 ⇒ 剩下这 15 列。
    expect(cols.map((c, i) => (c.align === 'center' ? i : -1)).filter((i) => i >= 0)).toEqual([
      6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
    ])
  })

  it('表格级常量：分页 limits 取 layPage.render 的 6 项（L2690），不是 treeTable 里那条死配置 [20,40]', () => {
    expect([...PAGE_LIMITS]).toEqual([10, 20, 30, 40, 50, 100])
  })
})

/**
 * 第 28 列「操作」= **纯图标按钮**（文案只在 `lay-tips` 里，而 `lay-tips` 我们不渲染）。
 * 判据 = `.layui-table-body tr:first-child td:last-child` 里的按钮序列（数量 / 顺序 / 类名）。
 */
describe('第 28 列「操作」= 对标 btnfun 的基础图标按钮（含按游戏互斥的「加入团」；G2 起「短信」已删）', () => {
  const row = {
    order_no: '20260920213215233029',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-20T21:32:15+08:00',
  }
  const render = (isGroupGame: boolean): string =>
    (
      buildOrderCols(
        () => 'G',
        () => '',
        { isGroupGame: () => isGroupGame },
      )[22]!.templet as (d: typeof row) => string
    )(row)
  /** 把 `<a …>…</a>` 拆成 `class` 列表（顺序即渲染顺序）。 */
  const classes = (html: string): string[] =>
    [...html.matchAll(/<a class="([^"]+)"/g)].map((m) => m[1]!)

  it('非魔兽/诛仙世界（默认「全部游戏」）：3 颗（短信已按 2026-10-28 业务方要求摘掉）', () => {
    // 「短信」不在（⛔ 别再加回来）；无推送行也没有「协商撤销」。
    expect(classes(render(false))).toEqual([
      'layui-btn layui-btn-xs layui-btn-danger submitEdit',
      'layui-btn layui-btn-xs layui-btn-warm throwEdit',
      'layui-btn layui-btn-xs layui-btn-normal pushJdpEdit',
    ])
  })

  it('魔兽世界/诛仙世界：4 颗（基础 3 颗 + 加入团），且**没有**「发短信」', () => {
    expect(classes(render(true))).toEqual([
      'layui-btn layui-btn-xs layui-btn-danger submitEdit',
      'layui-btn layui-btn-xs layui-btn-warm throwEdit',
      'layui-btn layui-btn-xs joinTask',
      'layui-btn layui-btn-xs layui-btn-normal pushJdpEdit',
    ])
  })

  it('无推送行：只有 3 颗纯图标按钮，**没有**文案节点', () => {
    const html = render(false)
    // 去掉标签后剩下的可见文本必须是空的（「协商撤销」只在已接手时出现）
    expect(html.replace(/<[^>]*>/g, '').trim()).toBe('')
    expect([...html.matchAll(/<i class="layui-icon ([^"]+)"/g)].map((m) => m[1])).toEqual([
      'layui-icon-edit',
      'layui-icon-home',
      'layui-icon-website',
    ])
  })

  it('每颗都带对标的 `lay-tips`（文案唯一去处）与 `data-id`/`data-viewid`', () => {
    const html = render(false)
    expect([...html.matchAll(/lay-tips="([^"]+)"/g)].map((m) => m[1])).toEqual([
      '编辑订单',
      '发到内部接单大厅',
      '把这笔订单一键发到各大交易平台',
    ])
    expect([...html.matchAll(/data-id="([^"]+)"/g)].map((m) => m[1])).toEqual([
      '20260920213215233029',
      '20260920213215233029',
      '20260920213215233029',
    ])
    // 对标那两颗的 `data-fee` / `data-recMan` 取 `recFee` / `recManName`，我们的列表载荷没有 → 不编。
    expect(html).not.toContain('data-fee')
    expect(html).not.toContain('data-recMan')
  })

  it('不传 opts 时按「非魔兽/诛仙世界」渲染（默认参数不改变既有调用方的行为）', () => {
    const t = buildOrderCols(
      () => 'G',
      () => '',
    )[22]!.templet as (d: typeof row) => string
    expect(classes(t(row))).toContain('layui-btn layui-btn-xs layui-btn-normal pushJdpEdit')
    // G2：短信那颗已删（⛔ 别再加回来）
    expect(t(row)).not.toContain('sendsmsorderbtn')
  })
})

/**
 * 管理订单页「操作」列的口径：
 *
 * - 常规三颗：编辑 / 退回大厅 / 一键发单到交易平台；
 * - **已抛未接手**（远端 11 / 未知）时**多一颗「从所有平台撤回」绿箭头**；
 * - 「加款 / 重发 / 协商撤销」整组搬到「第三方平台订单管理」页；短信已删；
 * - 没箭头可画时（已接手 / 终态 / 撤销在途）仍画一行可见说明，但文案要说**今天真成立**的话：
 *   「本页不提供平台写动作；需要操作请到『第三方平台订单管理』页处理」。
 *
 * 判据 = **每档各自的确切集合**（⛔ 不用「恰好三颗」这种一刀切），四档逐档断言 class + `lay-tips`；
 * 同时断言被搬走的那三颗（加款 / 重发 / 协商撤销）与短信的字面 / 类名一个都不出现。
 */
describe('2026-10-28 最终定案：操作列按行状态的确切集合（三颗 + 未接手时撤回箭头）', () => {
  const BASE = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
  }
  const push = (overrides: Partial<PlatformPushSummary> = {}): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 11,
    remote_cancel_status: 10,
    is_active: true,
    ...overrides,
  })
  const render = (overrides: Record<string, unknown> = {}): string =>
    (buildOrderCols(() => 'G', () => '')[22]!.templet as (d: Record<string, unknown>) => string)({
      ...BASE,
      ...overrides,
    })
  /** 每颗 `<a>` 的 class + `lay-tips`（顺序 = 渲染顺序）。 */
  const buttons = (html: string): Array<{ cls: string; tip: string }> =>
    [...html.matchAll(/<a class="([^"]+)"[^>]*lay-tips="([^"]+)"/g)].map((m) => ({
      cls: m[1]!,
      tip: m[2]!,
    }))
  const EDIT = { cls: 'layui-btn layui-btn-xs layui-btn-danger submitEdit', tip: '编辑订单' }
  const THROW = { cls: 'layui-btn layui-btn-xs layui-btn-warm throwEdit', tip: '发到内部接单大厅' }
  const CANCEL = {
    cls: 'layui-btn layui-btn-xs layui-btn-normal pushJdpCancel',
    tip: '把这笔订单从所有第三方交易平台撤回',
  }
  const PUSH = {
    cls: 'layui-btn layui-btn-xs layui-btn-normal pushJdpEdit',
    tip: '把这笔订单一键发到各大交易平台',
  }

  /** 四档：期望的按钮集合（**只含按钮**，⛔ 没有状态文字）。 */
  const CASES: Array<[string, Record<string, unknown>, Array<{ cls: string; tip: string }>]> = [
    ['未抛（无推送）', {}, [EDIT, THROW, PUSH]],
    [
      '已抛未接手（远端 11）',
      { status: 'IN_PROGRESS', platform_push: push({ remote_status: 11 }) },
      [EDIT, THROW, CANCEL, PUSH],
    ],
    [
      '已抛未接手（远端未知 null）',
      { status: 'IN_PROGRESS', platform_push: push({ remote_status: null }) },
      [EDIT, THROW, CANCEL, PUSH],
    ],
    [
      '已抛已接手（远端 12）',
      { status: 'IN_PROGRESS', platform_push: push({ remote_status: 12 }) },
      [EDIT, THROW, PUSH],
    ],
    [
      '终态（远端 17 已结算）',
      { status: 'COMPLETED', platform_push: push({ remote_status: 17 }) },
      [EDIT, THROW, PUSH],
    ],
  ]

  it.each(CASES)('%s ⇒ 确切集合（逐颗 class + 文案），且**只含按钮**', (_label, overrides, expected) => {
    const html = render(overrides)
    expect(buttons(html)).toEqual(expected)
    // 操作列**不许**有任何状态文字（`tt-push-terminal` 那种灰字整段不在）。
    expect(html.replace(/<[^>]*>/g, '')).toBe('')
    expect(html).not.toContain('tt-push-terminal')
    expect(html.includes('不能再撤回或协商撤销')).toBe(false)
  })

  /** 被搬走的三颗 + 早已删除的短信：字面与类名都不该在本页出现。 */
  const BANNED = [
    '加款',
    '重发',
    '协商撤销',
    '短信',
    '发送短信',
    'sendsmsorderbtn',
    'pushJdpAmount',
    'pushJdpRepush',
    'pushJdpRevoke',
  ]
  it.each(CASES)('%s ⇒ 加款 / 重发 / 协商撤销 / 短信一个字都不出现', (_label, overrides) => {
    const html = render(overrides)
    for (const word of BANNED) expect(html.includes(word), `不该出现「${word}」`).toBe(false)
  })

  it('平台订单全部结束（`is_active=false`）⇒ 操作列按未抛单（3 颗、无撤回箭头）', () => {
    const ended = { ...push({ remote_status: 11 }), is_active: false }
    const html = render({ platform_push: ended })
    expect(buttons(html)).toEqual([EDIT, THROW, PUSH])
    expect(html).not.toContain('pushJdpCancel')
    // 但三角仍在（历史可查）——见 B1 那一组的颜色断言。
    const orderCell = (
      buildOrderCols(() => 'G', () => '')[1]!.templet as (d: Record<string, unknown>) => string
    )({ ...BASE, platform_push: ended })
    expect(orderCell).toContain('tree_btn')
  })

  it('撤回箭头只在「已抛且未接手」那档出现：已接手 / 终态 / 撤销在途 / 已关闭都不给', () => {
    expect(pushRowAction(push({ remote_status: 11 }))).toBe('withdraw')
    expect(pushRowAction(push({ remote_status: null }))).toBe('withdraw')
    for (const status of [12, 13, 14, 15]) {
      expect(pushRowAction(push({ remote_status: status })), `remote_status=${status}`).toBe('none')
    }
    expect(pushRowAction(push({ remote_status: 17 }))).toBe('none')
    expect(pushRowAction(push({ remote_status: 16 }))).toBe('none')
    expect(pushRowAction(push({ remote_status: 13, remote_cancel_status: 16 }))).toBe('none')
    expect(pushRowAction(push({ remote_status: 13, remote_cancel_status: 11 }))).toBe('none')
    expect(pushRowAction(push({ remote_status: 13, remote_cancel_status: 14 }))).toBe('none')
    expect(pushRowAction(push({ state: 'closed' }))).toBe('none')
    expect(pushRowAction(null)).toBe('none')
    expect(pushRowAction(undefined)).toBe('none')
  })

  /**
   * `Status=18`（已下架）**不许画撤回箭头**：人工撤回对 18 必被后端拒
   * （`dailiantong_actions.py` 的「本系统不代发撤回」）⇒ 点了必失败的入口不摆按钮。
   * 判据打在纯函数与渲染产物两处。
   */
  it('⭐ L7：远端 18（已下架）⇒ 不给撤回箭头（人工撤回必被后端拒）', () => {
    expect(pushRowAction(push({ remote_status: 18 }))).toBe('none')
    const html = render({ platform_push: push({ remote_status: 18 }) })
    expect(html).not.toContain(PUSH_JDP_CANCEL_CLASS)
  })

  /**
   * 本页两颗平台写入口（发单地球 `.pushJdpEdit` / 撤回箭头 `.pushJdpCancel`）
   * 渲染挂 `order.platform_write`（fail-closed）。后端另一条线才加这个节点 ⇒ 今天走降级档
   * （按 `order.list`、身份取不到照画），见 `@/layui-island/perms::canPlatformWrite`。
   */
  it('⭐ 平台写入口挂 `order.platform_write`：没权限 ⇒ 地球与箭头都不画；有 ⇒ 照旧', () => {
    setPlatformWriteNodeShipped(true)
    // 没有该节点（但持 order.list，证明挡住它的不是页面级节点）⇒ 两颗都不画
    applyPlatformWritePermission({ menu_permissions: ['order.list'] })
    const denied = render({
      status: 'IN_PROGRESS',
      platform_push: push({ remote_status: 11 }),
    })
    expect(denied).not.toContain(PUSH_JDP_CLASS)
    expect(denied).not.toContain(PUSH_JDP_CANCEL_CLASS)
    // 本地入口不受牵连（编辑 / 发大厅仍在）
    expect(denied).toContain('submitEdit')
    expect(denied).toContain('throwEdit')

    // 有该节点 ⇒ 两颗都回来（与升级前逐字一致）
    applyPlatformWritePermission({
      menu_permissions: ['order.list', 'order.platform_write'],
    })
    const allowed = render({
      status: 'IN_PROGRESS',
      platform_push: push({ remote_status: 11 }),
    })
    expect(allowed).toContain(PUSH_JDP_CLASS)
    expect(allowed).toContain(PUSH_JDP_CANCEL_CLASS)
  })

  it('⭐ 降级档（后端还没有这个节点）⇒ 按 `order.list`：有就照画（升级前行为）', () => {
    applyPlatformWritePermission({ menu_permissions: ['order.list'] })
    const html = render({ platform_push: push({ remote_status: 11 }) })
    expect(html).toContain(PUSH_JDP_CANCEL_CLASS)
  })
})

/**
 * 「状态」列对**已抛到代练通的单**显示**平台综合状态**（判据只看行数据 `platform_push`：
 * 有效推送 = 平台状态；未抛 = 本地 8 档）。颜色沿用本页 `STATUS_COLORS` 的既有色档（⛔ 不新造颜色）。
 */
describe('「状态」列：已抛单显示平台综合状态（未抛单仍是本地状态）', () => {
  const BASE = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'IN_PROGRESS',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
  }
  const push = (overrides: Partial<PlatformPushSummary> = {}): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 11,
    remote_cancel_status: 10,
    is_active: true,
    ...overrides,
  })
  const render = (overrides: Record<string, unknown> = {}): string =>
    (buildOrderCols(() => 'G', () => '')[6]!.templet as (d: Record<string, unknown>) => string)({
      ...BASE,
      ...overrides,
    })

  it('已抛 + `Status=16` / `CancelStatus=11` ⇒ 「撤销中」（不是本地的「代练中」）', () => {
    const html = render({
      platform_push: push({ remote_status: 16, remote_cancel_status: 11 }),
    })
    expect(html).toContain('>撤销中</a>')
    expect(html).not.toContain('>代练中</a>')
    expect(html).toContain(`background-color:${STATUS_COLORS['撤销中']}`)
  })

  it('已抛 + `Status=12` ⇒ 「正在代练」（平台词，不是本地「代练中」）', () => {
    const html = render({ platform_push: push({ remote_status: 12 }) })
    expect(html).toContain('>正在代练</a>')
    expect(html).toContain(`background-color:${STATUS_COLORS['代练中']}`)
  })

  it('已抛 + `CancelStatus=12`（协商已处理）⇒ 「已撤销」', () => {
    const html = render({
      platform_push: push({ remote_status: 16, remote_cancel_status: 12 }),
    })
    expect(html).toContain('>已撤销</a>')
    expect(html).toContain(`background-color:${STATUS_COLORS['已撤销']}`)
  })

  it('已抛 + `Status=17` ⇒ 「已结算」', () => {
    const html = render({ platform_push: push({ remote_status: 17 }) })
    expect(html).toContain('>已结算</a>')
    expect(html).toContain(`background-color:${STATUS_COLORS['已完成']}`)
  })

  it('平台订单全部结束（`is_active=false`）⇒ 内部状态（本地已退单），不显示平台「已撤销」', () => {
    const html = render({
      status: 'REFUNDED',
      platform_push: push({
        remote_status: 16,
        remote_cancel_status: 12,
        is_active: false,
      }),
    })
    expect(html).toContain('>已退单</a>')
    expect(html).not.toContain('>已撤销</a>')
    expect(html).toContain(`background-color:${STATUS_COLORS['已退单']}`)
  })

  it('未抛单不回归：`IN_PROGRESS` 仍是本地「代练中」', () => {
    const html = render()
    expect(html).toContain('>代练中</a>')
    expect(html).toContain(`background-color:${STATUS_COLORS['代练中']}`)
    expect(html).not.toContain('正在代练')
    expect(html).not.toContain('data-push')
  })

  it('title：已抛单写清「已抛平台的单显示平台状态」；未抛单仍是原句', () => {
    expect(render({ platform_push: push() })).toContain('已抛平台的单显示平台状态')
    expect(render()).not.toContain('已抛平台的单显示平台状态')
    expect(render()).toContain('点击可以更改订单状态')
  })
})

/**
 * 两个日期筛选的默认值 = 「今天 − 3 个自然月 / 今天」，格式 `yyyy-MM-dd HH:mm:ss`、日号保留。
 * 对标 JS 的 `laydate.render` 不带 `value` / `min` ⇒ 默认值只由前端算，⛔ 别自己加 `min`。
 */
describe('日期筛选默认值：yyyy-MM-dd HH:mm:ss + 近 3 个自然月（出处 HTML L211/L215）', () => {
  it('给定「今天」时逐字产出对标那两个值', () => {
    expect(defaultDateRange(new Date(2026, 8, 20, 13, 5, 0))).toEqual({
      sDate: '2026-06-20 00:00:00',
      eDate: '2026-09-20 23:59:59',
    })
  })

  it('日号保留、月减 3（不是「减 90 天」）；跨年也对', () => {
    expect(defaultDateRange(new Date(2027, 0, 31)).sDate).toBe('2026-10-31 00:00:00')
    // 3 月 31 日往回 3 个月落到 12 月 31 日（跨年）
    expect(defaultDateRange(new Date(2027, 2, 31)).sDate).toBe('2026-12-31 00:00:00')
  })

  it('两端格式都是 `yyyy-MM-dd HH:mm:ss`（月份/日/时补零）', () => {
    const r = defaultDateRange(new Date(2026, 0, 5))
    expect(r.sDate).toBe('2025-10-05 00:00:00')
    expect(r.eDate).toBe('2026-01-05 23:59:59')
  })
})

describe('订单状态徽标：15 个硬编码色 + 未知状态回落到对标那个自带分号的默认值', () => {
  it('我们 8 个状态的中文与颜色（label 来自后端 OrderStatus 的静态镜像，本页不做页面级覆盖）', () => {
    expect(statusLabel('UNASSIGNED')).toBe('未分配')
    expect(statusLabel('IN_PROGRESS')).toBe('代练中')
    expect(statusLabel('PENDING_REVIEW')).toBe('待验收')
    expect(statusLabel('COMPLETED')).toBe('已完成')
    expect(statusLabel('ISSUE')).toBe('问题单')
    expect(statusLabel('PAUSED')).toBe('已暂停')
    expect(statusLabel('REFUNDED')).toBe('已退单')
    expect(statusColor('代练中')).toBe('#219227')
  })

  it('WAITING_GRAB 的中文与后端/大厅页**一致**：待抢单（不搞页面级覆盖）', () => {
    // 后端 choices：`OrderStatus.WAITING_GRAB` = `"WAITING_GRAB", "待抢单"`
    // 大厅页用 `@/config/orderStatus` 的同一份镜像 ⇒ 这里必须同词。
    expect(statusLabel('WAITING_GRAB')).toBe('待抢单')
    // 徽标配色按**中文 label** 查表：文案是「待抢单」时取本页色表「待抢单」那一档 `#D15FE8`
    //（不是本页另一档「待上号」的 `#585858`——那一档留着不动）。
    expect(statusColor(statusLabel('WAITING_GRAB'))).toBe('#D15FE8')
  })

  it('行内状态徽标（「状态」列，第二批列后下标 6）渲染「待抢单」+ 本页色表里「待抢单」那一档', () => {
    const html = (buildOrderCols(() => 'G', () => '')[6]!.templet as (d: never) => string)({
      order_no: '20260920213215233029', source_order_no: '', status: 'WAITING_GRAB',
      game: 2, server: null, project_text: 'x', total_price: '1.00',
      customer_phone: '', is_urgent: false, created_at: '2026-09-20T21:32:15+08:00',
    } as never)
    expect(html).toContain('>待抢单</a>')
    expect(html).toContain(STATUS_COLORS['待抢单']!)   // 本页色表自己的「待抢单」档
    expect(html).not.toContain('待上号')
    // 第一属性必须是 `href="javascript:void(0)"`：
    // 少了它这条 `<a>` 既没有手型光标、键盘也 focus 不到（vendored layui.css 的裸 `a` 规则不含 cursor）。
    expect(html).toContain('href="javascript:void(0)"')
  })

  it('对标未知状态走 `#b0aeae;`（**带尾分号**，照抄 L2175 附近的默认值）', () => {
    expect(statusColor('没有这个状态')).toBe('#b0aeae;')
  })

  it('未知 enum 值原样透出（不静默丢成空）', () => {
    expect(statusLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW')
  })

  it('统计句（`#toolbarDemo` 的 `.total`）也写「待抢单」——同屏不许一处一个说法', () => {
    // 有意偏差（对标那里写「待上号」），登记在文件头第 12 条。
    const total = /<span style="color:#ff6a00" class="total">([\s\S]*?)<\/span>/.exec(EVIDENCE)?.[1]
    expect(total).toBeDefined()
    expect(total).toContain('待抢单0笔')
    expect(total).not.toContain('待上号')
  })

  it('「订单状态」筛选下拉的第一档也是「待抢单」（同屏不许一处一个说法，见文件头第 12 条）', () => {
    // 这个 select 一直可见、mount 时**不重建**它的 option、也不在 `NOT_OPEN_FILTERS` 里，
    // 且 `toOrderSearch` 用 `statusOfFilterValue` 把它的 value 反查成枚举名再送 `params.status`
    // ⇒ 必须与徽标/统计句同词。
    expect(ORDER_STA_SELECT).not.toBe('')
    expect(ORDER_STA_SELECT).toContain('<option value="待抢单">待抢单</option>')
    expect(ORDER_STA_SELECT).not.toContain('待上号')
  })
})

describe('筛选 → 我们的检索参数（`/api/business/orders/` 能吃的那几个）', () => {
  it('空筛选送空对象（页码/页长由 layui 的 `request:{pageName,limitName}` 负责）', () => {
    expect(toOrderSearch({}, [])).toEqual({})
  })

  it('游戏取的是**我们**的主键；已删的区服控件即使有值也不进参数（D-0093）', () => {
    expect(toOrderSearch({ gameName: '2' }, [])).toMatchObject({ game: 2 })
    // 区服控件已从证据件删除、`toOrderSearch` 的 `params.server` 映射一并删除 ⇒ 即使有人把值
    // 塞进来也不再送。⚠️ 类型里的 `server` **保留**：它是后端合法参数（`?server=<区服主键>`），
    // 类型不是「页面有几个控件」的镜像。
    expect(toOrderSearch({ gameServer: '7' }, []).server).toBeUndefined()
  })

  it('搜索1 → keyword；搜索1 为空时搜索2 → project_text（对标只用一个输入框）', () => {
    expect(toOrderSearch({ key1: ' 角色名 ' }, [])).toMatchObject({ keyword: '角色名' })
    expect(toOrderSearch({ key2: ' 原神 ' }, [])).toMatchObject({ project_text: '原神' })
    expect(toOrderSearch({ key1: '', key2: '原神' }, [])).toMatchObject({
      project_text: '原神',
    })
  })

  it('「加急单」勾选 → is_urgent:true；未勾选**不送**（我们的接口没有「只看不加急」这个控件）', () => {
    expect(toOrderSearch({}, ['isJiajiCheck'])).toMatchObject({ is_urgent: true })
    expect(toOrderSearch({}, []).is_urgent).toBeUndefined()
  })

  it('订单状态：已映射的中文 label → 枚举码（多选拼串逐 token、逗号连码）；未映射档位不进 status', () => {
    // 映射**在这个函数里**做（不是要求调用方先清洗）⇒ 任何调用方都送不出中文 `status`。
    // ⭐ 多选已实装：多选拼串**送** status（逐 token 反查）——「多选 ⇒ 不送」是错口径。
    expect(toOrderSearch({ orderSta: '代练中' }, [])).toEqual({ status: 'IN_PROGRESS' })
    expect(toOrderSearch({ orderSta: '未分配,待抢单' }, [])).toEqual({
      status: 'UNASSIGNED,WAITING_GRAB',
    })
    // 混选：只送已映射的码（未映射档位归 `usedNotOpenFilters` 点名提示）
    expect(toOrderSearch({ orderSta: '未分配,待完善' }, [])).toEqual({ status: 'UNASSIGNED' })
    // 全未映射 ⇒ 不发键
    expect(toOrderSearch({ orderSta: '异常中' }, [])).toEqual({})
    expect(toOrderSearch({ orderSta: '' }, []).status).toBeUndefined()
  })

  it('时间范围按本地日取整、送带时区偏移的 ISO 8601（后端 created_at 的 gte/lte）', () => {
    const p = toOrderSearch({ sDate: '2026-06-20 00:00:00', eDate: '2026-09-20 23:59:59' }, [])
    const offsetMin = -new Date(2026, 5, 20, 0, 0, 0).getTimezoneOffset()
    const sign = offsetMin < 0 ? '-' : '+'
    const abs = Math.abs(offsetMin)
    const pad = (n: number): string => String(n).padStart(2, '0')
    const offset = `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
    expect(p.date_from).toBe(`2026-06-20T00:00:00${offset}`)
    expect(p.date_to).toBe(`2026-09-20T23:59:59${offset}`)
    // 页码/页长**不在**这里（见上面那条）：layui 自己按 `page.curr` 发。
    expect(p.page).toBeUndefined()
    expect(p.page_size).toBeUndefined()
  })

  it('已删的订单标签控件即使有值也不进 tag（D-0093；类型里的 `tag` 保留，它是后端合法参数）', () => {
    expect(toOrderSearch({ orderLabel: '微' }, []).tag).toBeUndefined()
    expect(toOrderSearch({ orderLabel: 'NoLael' }, []).tag).toBeUndefined()
  })

  it('金额区间**现在真送参**（T9 实装 `price_min` / `price_max`）——默认值也照发（对标同形）', () => {
    // ⛔ 本条原先断言 `Object.keys(p)` 为空（那时后端 `OrderSearchSerializer` 没有金额字段）。
    // T9 起后端有了这两个参数 ⇒ 期望值按事实**加强**成「逐键送对值」，不是放宽。
    expect(toOrderSearch({ minPrice: '-999999', maxPirce: '99999999' }, [])).toEqual({
      price_min: '-999999',
      price_max: '99999999',
    })
  })
})

describe('toIsoDateTime 的边界', () => {
  it('裸日期按开始/结束日补时刻', () => {
    expect(toIsoDateTime('2026-09-20', false)).toContain('2026-09-20T00:00:00')
    expect(toIsoDateTime('2026-09-20', true)).toContain('2026-09-20T23:59:59')
  })

  it('认不出的形状回 undefined（不猜、不送 NaN 字符串）', () => {
    expect(toIsoDateTime('', false)).toBeUndefined()
    expect(toIsoDateTime('2026/09/20', false)).toBeUndefined()
  })
})

describe('空态文案区分「没有匹配」与「还没有订单」', () => {
  it('没有筛选且没有数据 → 还没有订单', () => {
    expect(emptyStateText(0, { page: 1, page_size: 20 })).toBe('还没有订单')
  })

  it('筛过且没有数据 → 没有匹配的订单', () => {
    expect(emptyStateText(0, { page: 1, page_size: 20, keyword: 'x' })).toBe('没有匹配的订单')
  })

  it('有数据时没有空态文案', () => {
    expect(emptyStateText(3, { page: 1, page_size: 20 })).toBe('')
  })

  it('分页参数不算「筛过」', () => {
    expect(hasActiveFilters({ page: 2, page_size: 50 })).toBe(false)
    expect(hasActiveFilters({ page: 1, page_size: 20, is_urgent: true })).toBe(true)
  })
})

describe('「尚未开放」清单：删控件（D-0093）后剩下的那些', () => {
  it('筛选：一条都不剩（operator / keytid 两块已真删）；其余筛选都已实装', () => {
    // 恰好集合（⛔ 不许放宽成 `toContain`）：历次删控件（D-0093 等）与接线后，
    // 本表今天为**空**——所有筛选或已实装、或已真删。
    // ⚠️ `server` / `tag` 仍是后端合法参数，只是页面上不再有控件送它们。
    expect(NOT_OPEN_FILTERS.map((f) => f.name)).toEqual([])
  })

  it('工具栏 11 颗里 11 颗是真功能（refresh / setRecMan / changeStaBtn / changetypesBtn / changeOrderPriceBtn / refundOrderBtn / setOrderJSBtn / changeOrderThrowPriceBtn / delOrderBtn / builderSellEmpOrderBtn / exportBtn）', () => {
    // 恰好集合（⛔ 不许放宽成 `toContain`）：历次删控件与接真后，本表今天为**空**（11 颗全是真功能）。
    expect(NOT_OPEN_TOOLBAR).toEqual([])
    // 11 颗真功能一颗都不在「尚未开放」里（各自在 `onToolbarClick` 的 `action ===` 分支接真），
    // **不再**落到末端那句「尚未开放」提示。
    for (const real of [
      'refresh',
      'setRecMan',
      'changeStaBtn',
      'changetypesBtn',
      'changeOrderPriceBtn',
      'refundOrderBtn',
      'setOrderJSBtn',
      'changeOrderThrowPriceBtn',
      'delOrderBtn',
      'builderSellEmpOrderBtn',
      'exportBtn',
    ]) {
      expect(NOT_OPEN_TOOLBAR).not.toContain(real)
    }
  })

  /**
   * 台账复核用例：两份清单今天的恰好集合（筛选 0 条 / 工具栏 0 条）。历次删控件与接真
   * 都只改**期望值**，⛔ 判据仍是 `toEqual` 恰好集合，**不许**放宽成 `toContain`
   * （改数组的任一项都会红）。
   */
  it('这两份未开放清单的恰好集合（筛选 0 条 / 工具栏 0 条）', () => {
    expect(NOT_OPEN_FILTERS).toHaveLength(0)
    expect(NOT_OPEN_TOOLBAR).toHaveLength(0)
    expect(NOT_OPEN_FILTERS.map((f) => f.name)).toEqual([])
    expect([...NOT_OPEN_TOOLBAR]).toEqual([])
  })
})

describe('T9：收款状态筛选 → `payment_status`（spec §7 第 7 条 / §8 第 30 条）', () => {
  it('证据件里的三档中文 value 映射成三个码', () => {
    expect(toOrderSearch({ setSta: '未结算' }, [])).toMatchObject({ payment_status: 'UNSETTLED' })
    expect(toOrderSearch({ setSta: '结算中' }, [])).toMatchObject({ payment_status: 'SETTLING' })
    expect(toOrderSearch({ setSta: '已结算' }, [])).toMatchObject({ payment_status: 'SETTLED' })
  })

  it('不选 / 空串 / 映射不到的取值 ⇒ **不发** `payment_status`', () => {
    expect(toOrderSearch({}, []).payment_status).toBeUndefined()
    expect(toOrderSearch({ setSta: '' }, []).payment_status).toBeUndefined()
    expect(toOrderSearch({ setSta: '随便写的' }, []).payment_status).toBeUndefined()
  })

  it('原型链键（`constructor` / `__proto__` / `toString`）也**不发**（普通对象字面量的自有键判据，同 T7 评审 MF-1）', () => {
    expect(toOrderSearch({ setSta: 'constructor' }, []).payment_status).toBeUndefined()
    expect(toOrderSearch({ setSta: '__proto__' }, []).payment_status).toBeUndefined()
    expect(toOrderSearch({ setSta: 'toString' }, []).payment_status).toBeUndefined()
  })

  it('收款状态不再进「尚未开放」提示（它现在真接后端）', () => {
    expect(usedNotOpenFilters({ setSta: '已结算' }, [], { setSta: '0' })).not.toContain('收款状态')
  })

  it('`setSta` 不在 `NOT_OPEN_FILTERS` 里（恰好集合的出口对照）', () => {
    expect(NOT_OPEN_FILTERS.map((f) => f.name)).not.toContain('setSta')
  })
})

/**
 * 「尚未开放」的提示只能在**用户真的动过**那个控件时出现。
 *
 * 证据件里那些控件自带非空默认值（`recType/bussType/kefuType/showPriceType/selectSeach1` = `'0'`、
 * `minPrice` = `'-999999'`、`maxPirce` = `'99999999'`），用 `filled()` 单判会在**每次** reload 时弹
 * 「…筛选尚未开放，本次已忽略」——而 `layer.msg` 后来的覆盖先前的，成功提示会被这句警告顶掉。
 */
describe('usedNotOpenFilters：只报「与首屏默认值不同」的尚未开放筛选', () => {
  /**
   * 证据件里这几个控件的首屏默认值。
   * ⚠️ `key4Type` / `key4` / `gameAcc` 已随控件删除（D-0093）从本对象里去掉——它们的登记条目
   * 也已删，留着只会是一份指向不存在控件的「首屏默认值」。
   */
  const INITIAL = {
    recType: '0',
    bussType: '0',
    kefuType: '0',
    showPriceType: '0',
    selectSeach1: '0',
    minPrice: '-999999',
    maxPirce: '99999999',
  }

  it('全默认（用户什么都没动）→ 一条都不报', () => {
    expect(usedNotOpenFilters(INITIAL, [], INITIAL)).toEqual([])
  })

  it('本批（T9）实装的筛选**一条都不报**——它们已真送参', () => {
    // 这几个已接上后端参数 ⇒ **不再**属于「尚未开放」，报出来反而会把成功提示顶掉（本 describe 的立论）。
    expect(usedNotOpenFilters({ ...INITIAL, bussType: '1' }, [], INITIAL)).toEqual([])
    expect(usedNotOpenFilters({ ...INITIAL, minPrice: '100' }, [], INITIAL)).toEqual([])
    expect(usedNotOpenFilters({ ...INITIAL, minPrice: '100', maxPirce: '200' }, [], INITIAL)).toEqual(
      [],
    )
    // 「未安排」已实装（`is_leak`）⇒ 勾了不报（它走 `toOrderSearch` 的映射）。
    expect(usedNotOpenFilters(INITIAL, ['viewLeakOrder'], INITIAL)).toEqual([])
    // ⭐ 价格口径 `3 接发组合` / `4 搜亏本单` 两档都**真送参**
    // （`3` 走弹层那 4 格、`4` 只发档位本身）⇒ 报「尚未开放」就是**说假话**。
    // ⛔ 不是删掉判据：`4` 档 + 主栏区间被改过那一支**仍然**必须说出来（「订单状态」describe
    // 里的合并提示判据钉的就是它）。
    expect(usedNotOpenFilters({ ...INITIAL, showPriceType: '3' }, [], INITIAL)).toEqual([])
    expect(usedNotOpenFilters({ ...INITIAL, showPriceType: '4' }, [], INITIAL)).toEqual([])
  })

  it('本表为空之后：把值写进那两个已删控件的名字，也一条都不报', () => {
    // `operator` / `keytid` 已真删 ⇒ 这两个名字今天**不在**清单里，写进值也不报
    // （`usedNotOpenFilters` 只遍历清单里的控件）。
    // ⛔ 断言没有放宽：仍是 `toEqual` 恰好集合（空集），将来谁把这两条加回清单，这条会红。
    expect(usedNotOpenFilters({ ...INITIAL, keytid: 'TP-000001' }, [], INITIAL)).toEqual([])
    expect(usedNotOpenFilters({ ...INITIAL, operator: '测试派单人甲' }, [], INITIAL)).toEqual([])
    expect(usedNotOpenFilters(INITIAL, [], INITIAL)).toEqual([])
  })

  /**
   * 「订单状态」不在 `NOT_OPEN_FILTERS` 里（它是真接后端的筛选）。多选拼串逐 token 反查
   * （`splitOrderStatusSelection`），**已映射的档位真送参** ⇒ 只报
   * **未映射的档位**、且**点名档位本身**（多选已实装，再带「（多选）」字样就是说假话）。
   */
  it('「订单状态」：未映射档位点名档位；多选全映射不报；混选只报未映射那几档', () => {
    const INITIAL_STA = { ...INITIAL, orderSta: '' }
    // 单个未映射档位 ⇒ 点名档位本身
    expect(usedNotOpenFilters({ ...INITIAL_STA, orderSta: '异常中' }, [], INITIAL_STA)).toEqual([
      '异常中',
    ])
    // 多选且全部已映射 ⇒ 一条都不报（真送 `status=UNASSIGNED,WAITING_GRAB`）
    expect(
      usedNotOpenFilters({ ...INITIAL_STA, orderSta: '未分配,待抢单' }, [], INITIAL_STA),
    ).toEqual([])
    // 混选：只报未映射的那几档、按选中顺序
    expect(
      usedNotOpenFilters(
        { ...INITIAL_STA, orderSta: '未分配,待完善,异常中' },
        [],
        INITIAL_STA,
      ),
    ).toEqual(['待完善', '异常中'])
    // 已映射单选 / 全部（首屏默认）⇒ 不报
    expect(usedNotOpenFilters({ ...INITIAL_STA, orderSta: '代练中' }, [], INITIAL_STA)).toEqual([])
    expect(usedNotOpenFilters(INITIAL_STA, [], INITIAL_STA)).toEqual([])
  })
})

/**
 * 「订单状态」筛选**真正生效**（计划 `2026-09-21-status-fix-residuals.md` Task 1）。
 *
 * 证据件的 option **value 是中文 label**（16 个档位 = 8 个有枚举对应 + 8 个没有），
 * formSelects 把它写进 `<input name="orderSta" class="xm-hide-input">` ⇒ `form.val('form')` 读到中文；
 * 而后端 `OrderSearchSerializer.status` **只认枚举名** ⇒ 页内逐 token 反查成枚举码；
 * 已映射的拼串送 `status`（后端收逗号串枚举码），未映射的不进 `status`、提示**点名档位**
 * （见文件头「已知差异 14」）。
 *
 * 断言走**真实调用点**（挂载整页 → 「编辑保存成功」消息 → `reload(currentPage)` → `collectSearch()`）；
 * `splitOrderStatusSelection` 是页内内部函数、不导出，所以测试不直接调它（另有 `toOrderSearch` 的
 * 独立单测直接钉映射契约）。
 * 期望值写字面值（E-0024），不拿 `ORDER_STATUS_LABELS` 自指。
 */
describe('「订单状态」筛选：中文档位 → 后端枚举名（R1）', () => {
  /** 8 个有枚举对应的档位：左 = 证据件 option value，右 = 后端枚举名（两边都是字面值）。 */
  const MAPPABLE: [string, string][] = [
    ['未分配', 'UNASSIGNED'],
    ['待抢单', 'WAITING_GRAB'],
    ['代练中', 'IN_PROGRESS'],
    ['待验收', 'PENDING_REVIEW'],
    ['已完成', 'COMPLETED'],
    ['问题单', 'ISSUE'],
    ['已暂停', 'PAUSED'],
    ['已退单', 'REFUNDED'],
  ]
  /** 8 个有 option、但 `OrderStatus` 里没有对应枚举的档位。 */
  const UNMAPPABLE: string[] = [
    '抢单中',
    '已超时',
    '待完善',
    '撤销中',
    '已撤销',
    '仲裁中',
    '已仲裁',
    '异常中',
  ]
  /** 未映射档位的提示**点名档位本身**。 */
  const notOpenMessageOf = (tier: string) => `「${tier}」筛选尚未开放，本次已忽略`
  /** 「异常中」这类未映射档位与另一条提示同时被改 ⇒ 合并成**一句**。
   *  ⚠️ 两句用**不同模板**、由 `notOpenMessage` 用 `；` 连成**一条** msg
   *  （档 `4` **已经开放**，只是不吃金额区间 ⇒ 套「尚未开放」的模板就是说假话）。 */
  const combinedMessageOf = (tier: string) =>
    `${notOpenMessageOf(tier)}；${LOSS_PRICE_RANGE_IGNORED_TEXT}`
  /** 与「订单状态」并进同一条提示的那条**已实装但口径不吃区间**的筛选：
   *  档 `4` + 主栏下界被改过（`minPrice` 与 `INITIAL` 里的 `-999999` 不同）。
   *  判据仍是**恰好**两段合成一句（⛔ 不是放宽成 `toContain`）。 */
  const STILL_NOT_OPEN = { showPriceType: '4', minPrice: '100' }

  beforeEach(() => {
    LAYUI.msg.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.form.val.mockReturnValue({})
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
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<() => void> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    return mountOrderListPage(root)
  }

  /** 走本页真实的 `collectSearch()` 调用点之一：编辑保存成功 → `reload(currentPage)`。 */
  function reloadWith(values: Record<string, string>): void {
    LAYUI.form.val.mockReturnValue(values)
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: ORDER_SAVED_MESSAGE_TYPE, order_no: '20260920213215233029' },
        origin: window.location.origin,
      }),
    )
  }

  function reloadWithStatus(raw: string): void {
    reloadWith({ orderSta: raw })
  }

  /** 最近一次 `table.reload` 的 `where`（`status` 键的有无另用 `in` 判）。 */
  function lastWhere(): Record<string, unknown> {
    const calls = LAYUI.table.reload.mock.calls
    const call = calls[calls.length - 1]
    expect(call?.[0]).toBe('datatable')
    return (call?.[1] as { where: Record<string, unknown> } | undefined)?.where ?? {}
  }

  it.each(MAPPABLE)('「%s」→ 送出枚举名 %s', async (label, enumName) => {
    const cleanup = await mountPage()
    LAYUI.table.reload.mockClear()
    reloadWithStatus(label)
    expect(lastWhere().status).toBe(enumName)
    cleanup()
  })

  it('「全部」（`value=""`，首屏默认）→ 结果里没有 `status` 键，且不弹「尚未开放」', async () => {
    const cleanup = await mountPage()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    reloadWithStatus('')
    expect('status' in lastWhere()).toBe(false)
    expect(
      LAYUI.msg.mock.calls.filter((c) => String(c[0]).includes('筛选尚未开放')),
    ).toHaveLength(0)
    cleanup()
  })

  it.each(UNMAPPABLE)(
    '「%s」没有对应枚举 → 结果里没有 `status` 键 + 提示点名该档位',
    async (raw) => {
      const cleanup = await mountPage()
      LAYUI.table.reload.mockClear()
      LAYUI.msg.mockClear()
      // 同时把价格口径改到档 `4` 并动一下主栏区间（那一档不吃区间 ⇒ 必须说出来）：
      // 两段提示必须**合并成一句**，不是各弹一次。
      reloadWith({ orderSta: raw, ...STILL_NOT_OPEN })
      expect('status' in lastWhere()).toBe(false)
      expect(LAYUI.msg).toHaveBeenCalledWith(combinedMessageOf(raw), { time: 1500 })
      expect(
        LAYUI.msg.mock.calls.filter((c) => String(c[0]).includes('筛选尚未开放')),
      ).toHaveLength(1)
      cleanup()
    },
  )

  it('⭐ 多选全映射（formSelects 用 `,` 拼串）→ `status` 送逗号串码，且不弹「尚未开放」', async () => {
    const cleanup = await mountPage()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    reloadWithStatus('未分配,待抢单')
    expect(lastWhere().status).toBe('UNASSIGNED,WAITING_GRAB')
    // 这条链路（编辑保存成功 → reload）本身会先弹「保存成功」⇒ 只钉「没有筛选提示」那一类。
    expect(
      LAYUI.msg.mock.calls.filter((c) => String(c[0]).includes('筛选尚未开放')),
    ).toHaveLength(0)
    cleanup()
  })

  it('⭐ 多选部分映射 → 只送已映射的码；未映射档位点名进合并提示（仍只发一条）', async () => {
    const cleanup = await mountPage()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()
    // 已映射的「未分配/待抢单」照送，未映射的「待完善」点名提示；同时带上档 `4` 那句
    // 完整的话 ⇒ 两段必须**合并成一句**（`;` 相连），⛔ 不是各弹一次。
    reloadWith({ orderSta: '未分配,待完善,待抢单', ...STILL_NOT_OPEN })
    expect(lastWhere().status).toBe('UNASSIGNED,WAITING_GRAB')
    expect(LAYUI.msg).toHaveBeenCalledWith(combinedMessageOf('待完善'), { time: 1500 })
    // 与相邻用例同一条判据：这句提示**只准发一次**（`layer.msg` 后来的覆盖先前的，
    // 多发一次就把别的反馈顶掉）。
    expect(LAYUI.msg.mock.calls.filter((c) => String(c[0]).includes('筛选尚未开放'))).toHaveLength(
      1,
    )
    cleanup()
  })
})

/**
 * R2：页码 / 每页条数走 layui 的**真实出口**（计划 `2026-09-21-status-fix-residuals.md` Task 2）。
 *
 * 缺陷（E-0037）：`table.on('page(datatable)')` 在 vendored layui 2.13.9 里**不存在**
 * （`grep -c 'page('` = 0）⇒ 那个 handler 一次都不发：`currentSize` 恒 20、纯翻页不改 `currentPage`，
 * 于是用户选了「50 条/页」后任何 `reload` 都会把它顶回 20。真实出口有两个：
 *  - **页码**：`table.render` 的 `done(res, curr, count)`（vendored `public/layui/layui.js` L1 压缩单行、
 *    字符偏移 292034：`o.done(e, i, e[r.countName], t)`），layui **每次取数后**都调它；
 *  - **每页条数**：`table.getOptions(id).page.limit`（同文件偏移 321568 `F.getOptions=l`，`l` 返回
 *    `H.config[id]`，偏移 272624）。用户选的「条/页」由 laypage **就地**写进同一个 `page` 对象：
 *    表格把 `config.page` 按引用交给 laypage，laypage 构造 `a.config=e||{}`（偏移 32309）不复制，
 *    改页长时执行 `i.limit=e`（偏移 35833）。
 *
 * ⚠️ 断言打在**真实调用点**上：翻页驱动 `done(...)`（不是手调 handler），刷新走真实的
 * 「编辑保存成功」消息 → `reload(currentPage)`；期望值写字面值（E-0024）。
 */
describe('R2：页码 / 每页条数走 layui 真实出口（done + getOptions）', () => {
  const ORDER_NO = '20260920213215233029'

  beforeEach(() => {
    LAYUI.msg.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.form.val.mockReturnValue({})
    LAYUI.table.getOptions.mockReturnValue({ page: { limit: 20 } })
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
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    return { root, cleanup }
  }

  /** 走本页真实的 `reload(currentPage)` 调用点：编辑保存成功 → `reload(currentPage)`。 */
  function savedMessageReload(): void {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: ORDER_SAVED_MESSAGE_TYPE, order_no: ORDER_NO },
        origin: window.location.origin,
      }),
    )
  }

  /** 最近一次 `table.reload` 的 `page` 入参（`page.curr` / `page.limit`）。 */
  function lastReloadPage(): { curr?: number; limit?: number } {
    const calls = LAYUI.table.reload.mock.calls
    const call = calls[calls.length - 1]
    expect(call?.[0]).toBe('datatable')
    return (call?.[1] as { page: { curr?: number; limit?: number } }).page
  }

  it('页长：用户选「50 条/页」（getOptions 回 50）后走 reload(currentPage) → 仍发 page.limit=50', async () => {
    const { cleanup } = await mountPage()
    // 用户把「条/页」改成 50：laypage 就地把它写进 `config.page.limit` ⇒ `getOptions` 读到的就是 50。
    LAYUI.table.getOptions.mockReturnValue({ page: { limit: 50 } })
    LAYUI.table.reload.mockClear()

    savedMessageReload()

    expect(lastReloadPage().limit).toBe(50)
    cleanup()
  })

  it('页码：真人翻到第 3 页（done(res,3,40)）后走同一条刷新 → 仍发 page.curr=3', async () => {
    const { cleanup } = await mountPage()
    // 模拟用户点页码：layui 取完数后调 `done(res, 3, 40)`（第 2 参就是刚加载的页码）。
    triggerDone({}, 3, 40)
    LAYUI.table.reload.mockClear()

    savedMessageReload()

    expect(lastReloadPage().curr).toBe(3)
    cleanup()
  })

  it('回归：裸 reload()（工具栏「刷新」）仍回第 1 页——即使当前停在第 3 页', async () => {
    const { root, cleanup } = await mountPage()
    // 先翻到第 3 页，证明下面的 `curr=1` 不是「本来就没翻过」凑出来的。
    triggerDone({}, 3, 40)
    LAYUI.table.reload.mockClear()

    const refresh = root.querySelector<HTMLElement>('button[data-tb="refresh"]')
    expect(refresh, '证据件里找不到 [data-tb=refresh]').not.toBeNull()
    refresh!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(lastReloadPage().curr).toBe(1)
    cleanup()
  })

  it('幽灵事件已删：`table.on` 不再登记任何 `page(...)`（阳性哨兵证明 on() 本身接上了）', async () => {
    const { cleanup } = await mountPage()
    // 判据打在**登记出口**上：原实现会往 handler map 里塞一个 `page(datatable)` ⇒ 这条会红。
    expect([...LAYUI.tableHandlers.keys()].filter((k) => k.startsWith('page('))).toEqual([])
    // 阳性哨兵：本页确实调了 `table.on`/`form.on`——否则一张空 map 也能满足上面那条
    // （`on` 没被替身接上时，`page(...)` 自然也不在 map 里）。
    // ⚠️ 判据是**恰好集合**（改一个都会红）：`recType` 切候选名单 / `selectGame` 重取候选 /
    // `showPriceType` 选到档 `3` 弹 `.seachMutPriceDiv`、别的档收掉弹层 / `selectSeach1` 批量搜单号弹层。
    expect([...LAYUI.formHandlers.keys()]).toEqual([
      'checkbox(viewIsJiaJi)',
      'checkbox(viewLeakOrder)',
      'select(recType)',
      'select(selectGame)',
      'select(showPriceType)',
      'select(selectSeach1)',
    ])
    cleanup()
  })
})

/**
 * 第 2 列「订单号」与第 3 列「系统内置单号」的渲染 —— 业务方规则。
 *
 * 规则原文的口径：
 *  - **订单号列**：来源单号**非空**时显示 `source_order_no`；为空时显示系统订单号 `order_no`；
 *  - **系统内置单号列**：**无论如何都有系统内置单号** ⇒ **恒**显示 `order_no`，不受来源单号影响。
 *
 * ⚠️ 这两列**不是同一条规则**，别合并：订单号列会切到来源单号，内置单号列**永远**是 `order_no`。
 */
describe('第 2 列「订单号」按来源单号切换 + 第 3 列「系统内置单号」恒为 order_no', () => {
  const row = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
    // 显式标出类型，下面用 `{...row, platform_push: {…}}` 覆盖时不会被推断成 `null` 字面量。
    platform_push: null as PlatformPushSummary | null,
  }
  const cols = buildOrderCols(
    () => 'G',
    () => '',
  )
  const orderIdCol = cols[1]!
  const sysIdCol = cols[2]!
  const orderIdHtml = (d: typeof row): string =>
    (orderIdCol.templet as (r: typeof row) => string)(d)
  const sysIdHtml = (d: typeof row): string => (sysIdCol.templet as (r: typeof row) => string)(d)

  it('订单号列：来源单号非空 ⇒ 显示来源单号', () => {
    const html = orderIdHtml({ ...row, source_order_no: 'TB-2026-777' })
    expect(html).toContain('>TB-2026-777</a>')
    // 该列**不再**显示系统订单号（它是订单独的展示值）
    expect(html).not.toContain('>20260921000000000001</a>')
    // ⚠️ 但 `data-order` **仍是** `order_no`：它是行内点击（跳详情 / 改状态）用的标识，
    // 来源单号租户内不唯一，换成它会让操作指向错的单。
    expect(html).toContain('data-order="20260921000000000001"')
    expect(html).toContain('id="order_20260921000000000001"')
    // 这条 `<a>` 的 `href` 是字面值 `javascript:void(0);`——**带尾分号**
    // （状态徽标那处是不带分号的 `javascript:void(0)`，两处不是同一个字面值）。
    expect(html).toContain('href="javascript:void(0);"')
  })

  it('订单号列：来源单号为空 ⇒ 回落显示系统订单号', () => {
    const html = orderIdHtml({ ...row, source_order_no: '' })
    expect(html).toContain('>20260921000000000001</a>')
  })

  it('⭐ 订单号列：来源单号（**录入的自由文本**）过 `escapeHtml`——正文与 `data-viewId` 两处都转义（T5 顺手补的既有 XSS）', () => {
    // `Order.source_order_no` 是 `CharField(max_length=128)`、**没有字符集校验** ⇒
    // 它就是用户输入，进 anchor 的**正文**与 `data-viewId` 前必须转义（存储型 XSS 闸）。
    const html = orderIdHtml({ ...row, source_order_no: '<b>x</b>&"' })
    // ⛔ 四个字符都要转：`<` `>` `&` `"`（`escapeHtml` 的四条替换）。
    expect(html).toContain('>&lt;b&gt;x&lt;/b&gt;&amp;&quot;</a>')
    expect(html).toContain('data-viewId="&lt;b&gt;x&lt;/b&gt;&amp;&quot;"')
    // 反例：转义前那种「原样拼进去」的产物一个都不许在。
    expect(html).not.toContain('><b>x</b>')
    expect(html).not.toContain('data-viewId="<b>')
  })

  it('内置单号列：`field`/`title` 与对标 colsdata L2177 一致，且**恒**显示 order_no', () => {
    expect(sysIdCol).toMatchObject({
      field: 'orderId',
      title: '系统内置单号',
      width: 142,
      hide: true,
    })
    // 来源单号为空时：内置单号 = 订单号（且不是空串）
    const empty = sysIdHtml({ ...row, source_order_no: '' })
    expect(empty).not.toBe('')
    expect(empty).toContain('20260921000000000001')
    // 来源单号**非空**时：内置单号**仍然是**订单号（业务方：「无论如何都有系统内置单号」）
    const filled = sysIdHtml({ ...row, source_order_no: 'TB-2026-777' })
    expect(filled).toContain('20260921000000000001')
    expect(filled).not.toContain('TB-2026-777')
  })

  it('内置单号列形状照 sysorderIdRow：`.orderIDcopy` + `data-clipboard-text` + `#0094ff`', () => {
    const html = sysIdHtml(row)
    expect(html).toContain('class="orderIDcopy"')
    expect(html).toContain('data-clipboard-text="20260921000000000001"')
    expect(html).toContain('color:#0094ff')
    expect(html).toContain('lay-tips="这是系统内置单号，每笔单号不一样，单击可复制"')
  })
})

/**
 * 订单号列的绿色「通」徽标（`platform_push` 非空才画）。
 *
 * 判据分三组（⛔ 不只断言元素存在）：
 *  ① 两态：`null` ⇒ **什么都不画**；非空 ⇒ 徽标 + 悬停文案；
 *  ② 状态中文：11–17 逐码 + `state=closed` ⇒「已关闭」；
 *  ③ 边界：没有平台单号 ⇒「—」、序列号过 `escapeHtml`。
 *
 * ⚠️ **列定义不许动**：本文件同时钉住「订单号列还是 23 列里的第 2 列、templet 还是它」——
 * 加徽标的实现只允许在既有 templet 里拼字符串。
 */
describe('T11a：订单号列的「通」徽标（platform_push 两态 + 状态中文）', () => {
  const base = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
    platform_push: null as PlatformPushSummary | null,
  }
  const cols = buildOrderCols(
    () => 'G',
    () => '',
  )
  const cell = (overrides: Partial<typeof base>): string =>
    (cols[1]!.templet as (r: typeof base) => string)({ ...base, ...overrides })
  const push = (overrides: Partial<PlatformPushSummary> = {}): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 12,
    remote_cancel_status: null,
    is_active: true,
    ...overrides,
  })

  it('两态：platform_push = null ⇒ **不渲染任何东西**（不占位、不显示「-」）', () => {
    const html = cell({ platform_push: null })
    expect(html).not.toContain('layui-badge')
    expect(html).not.toContain('layui-bg-green')
    expect(html).not.toContain('>通</span>')
    expect(html).not.toContain('平台单号')
    // 订单号本体的渲染逐字不变（徽标是纯加法）。
    expect(html).toContain('class="orderdetails"')
  })

  it('非空 ⇒ 绿色 layui 徽标「通」+ 悬停文案（代练通 · 平台单号 xxx · 状态 正在代练）', () => {
    const html = cell({ platform_push: push() })
    expect(html).toContain('class="layui-badge layui-bg-green"')
    expect(html).toContain('>通</span>')
    expect(html).toContain(
      'title="代练通 · 平台单号 19000000000000000201 · 状态 正在代练"',
    )
    // 徽标在单号链接**之前**（与「急」/「拆」同一串）。
    expect(html.indexOf('>通</span>')).toBeLessThan(html.indexOf('class="orderdetails"'))
  })

  it('状态中文逐码对上（11–17；14 逐字取码表「订单异常」）', () => {
    const expected: Record<number, string> = {
      11: '未接手',
      12: '正在代练',
      13: '等待验收',
      14: '订单异常',
      15: '已锁定',
      16: '撤销中',
      17: '已结算',
    }
    for (const [code, label] of Object.entries(expected)) {
      const html = cell({ platform_push: push({ remote_status: Number(code) }) })
      expect(html, `remote_status=${code}`).toContain(`· 状态 ${label}"`)
    }
  })

  it.each([
    ['unknown', 'layui-bg-orange'],
    ['failed', 'layui-bg-red'],
    ['closed', 'layui-bg-gray'],
  ] as const)('state=%s ⇒ 徽标颜色变成 %s（⛔ 不再一律绿色），title 明细保留', (state, cls) => {
    const html = cell({
      platform_push: push({ state, remote_status: null, remote_cancel_status: null }),
    })
    expect(html).toContain(`class="layui-badge ${cls}"`)
    expect(html).not.toContain('layui-bg-green')
    // 徽标文字仍是一个「通」（表示已抛到代练通），明细在 title 里。
    expect(html).toContain('>通</span>')
    expect(html).toContain('title="代练通 · 平台单号 19000000000000000201 · 状态 ')
  })

  it('state=closed ⇒ 显示「已关闭」（业务方 2026-10-27 口径）', () => {
    // 即使远端还留着旧状态，本地已删除也要说「已关闭」。
    const html = cell({ platform_push: push({ state: 'closed', remote_status: 12 }) })
    expect(html).toContain('· 状态 已关闭"')
  })

  it('没有平台单号 ⇒ 悬停里是「—」，⛔ 不拼一个假单号；序列号过 escapeHtml', () => {
    const noSerial = cell({
      platform_push: push({ state: 'pending', serial_no: '', remote_status: null }),
    })
    expect(noSerial).toContain('title="代练通 · 平台单号 — · 状态 发送中"')

    const xss = cell({ platform_push: push({ serial_no: '<b>&"' }) })
    expect(xss).toContain('平台单号 &lt;b&gt;&amp;&quot;')
    expect(xss).not.toContain('平台单号 <b>')
  })

  it('列定义一个字没动（仍是 23 列、订单号仍是第 2 列且带 templet）', () => {
    expect(cols).toHaveLength(23)
    expect(cols[1]).toMatchObject({ field: '_orderid', title: '订单号' })
    expect(typeof cols[1]!['templet']).toBe('function')
  })
})

/**
 * **抛过平台的单，订单号文字紫色**（`#D15FE8`；默认 `#0094ff` 蓝）。
 * 「抛过」的判据 = 列表载荷的 `platform_push` **非空**（`sent` / `closed` / `failed` 都算
 * —— 只要有过推送行，「通」徽标与三角就都在，颜色同理）。
 */
describe('P5：订单号紫色只给「抛过平台」的单（色值取对标 #D15FE8）', () => {
  const base = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
    platform_push: null as PlatformPushSummary | null,
  }
  const cols = buildOrderCols(
    () => 'G',
    () => '',
  )
  const orderCell = (overrides: Partial<typeof base> = {}): string =>
    (cols[1]!.templet as (r: typeof base) => string)({ ...base, ...overrides })
  const push = (overrides: Partial<PlatformPushSummary> = {}): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 11,
    remote_cancel_status: null,
    is_active: true,
    ...overrides,
  })

  it('色值常量逐字取对标（⛔ 不拿别的紫代替）', () => {
    expect(ORDER_NO_PUSHED_COLOR).toBe('#D15FE8')
    expect(ORDER_NO_DEFAULT_COLOR).toBe('#0094ff')
  })

  it('无推送 ⇒ 默认蓝 `#0094ff`，⛔ 不出现紫色', () => {
    const html = orderCell()
    expect(html).toContain(`color:${ORDER_NO_DEFAULT_COLOR}`)
    expect(html).not.toContain(ORDER_NO_PUSHED_COLOR)
  })

  it.each([['已发出', 'sent'], ['已关闭', 'closed'], ['发送失败', 'failed']] as const)(
    '%s（state=%s）⇒ 订单号紫色（closed 仍算「抛过」）',
    (_label, state) => {
      const html = orderCell({ platform_push: push({ state }) })
      expect(html).toContain(`color:${ORDER_NO_PUSHED_COLOR}`)
      expect(html).not.toContain(`color:${ORDER_NO_DEFAULT_COLOR}`)
    },
  )

  it('显示来源单号时同样按「有没有推送」上色（两者互不影响）', () => {
    const pushed = orderCell({ source_order_no: 'TB-1', platform_push: push() })
    expect(pushed).toContain('>TB-1</a>')
    expect(pushed).toContain(`color:${ORDER_NO_PUSHED_COLOR}`)
    const plain = orderCell({ source_order_no: 'TB-1' })
    expect(plain).toContain(`color:${ORDER_NO_DEFAULT_COLOR}`)
  })

  it('第 3 列「系统内置单号」无推送 ⇒ 默认蓝 `#0094ff`（B1 起有推送时才转紫，见下一组）', () => {
    const html = (cols[2]!.templet as (r: typeof base) => string)(base)
    expect(html).toContain(`color:${ORDER_NO_DEFAULT_COLOR}`)
    expect(html).not.toContain(ORDER_NO_PUSHED_COLOR)
  })
})

/**
 * 发到平台的单，**订单号 / 系统内置单号 / 角色名 / 代练内容 / 角色信息** 五列文字都紫 `#D15FE8`；
 * 没有推送行的单五列都保持默认（前两列默认蓝 `#0094ff`，后三列**不打颜色**）。
 *
 * 「还在平台跑」的判据 = 列表载荷 `platform_push.is_active === true`
 * （全部结束后恢复普通内部订单样式）。
 * ⚠️ 后三列无推送那一档是**空串（不加色）**，不是蓝色 —— ⛔ 别「统一成蓝」。
 */
describe('B1：五列（订单号 / 系统内置单号 / 角色名 / 代练内容 / 角色信息）推送后都紫', () => {
  const base = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: '代练到 60 级',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
    game_role_name: '角色甲',
    platform_push: null as PlatformPushSummary | null,
  }
  const cols = buildOrderCols(
    () => '魔兽世界',
    () => '一区',
  )
  const push = (): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 11,
    remote_cancel_status: null,
    is_active: true,
  })
  /** 按 `field` 取那一列的 templet 渲染一行（⛔ 不按下标，下标会随删列漂移）。 */
  const cell = (field: string, overrides: Partial<typeof base> = {}): string => {
    const col = cols.find((c) => c.field === field)
    expect(col, `列定义里找不到 field=${field}`).toBeDefined()
    expect(col!.templet, `field=${field} 没有 templet`).toBeTypeOf('function')
    return (col!.templet as (r: typeof base) => string)({ ...base, ...overrides })
  }
  /** 五列的 `field`。 */
  const FIVE = ['_orderid', 'orderId', '_role', '_project', '_info'] as const

  it('有推送行 ⇒ 五列**每一列**都出现字面色值 `#D15FE8`', () => {
    for (const field of FIVE) {
      expect(cell(field, { platform_push: push() }), `列 ${field} 应当紫色`).toContain('#D15FE8')
    }
  })

  it('无推送 ⇒ 五列**一列都不许**出现紫色（前两列是默认蓝、后三列不打颜色）', () => {
    for (const field of FIVE) {
      expect(cell(field), `列 ${field} 不该有紫色`).not.toContain('#D15FE8')
    }
    // 前两列：默认蓝仍在
    expect(cell('_orderid')).toContain('#0094ff')
    expect(cell('orderId')).toContain('#0094ff')
    // 后三列：**不加 style**——钉住「别顺手统一成蓝」。
    expect(cell('_role')).not.toContain('color:')
    expect(cell('_project')).not.toContain('color:')
    expect(cell('_info')).not.toContain('color:')
  })

  it('平台订单全部结束（`is_active=false`）⇒ 五列恢复普通内部订单：一列都不紫', () => {
    const ended: PlatformPushSummary = { ...push(), is_active: false }
    for (const field of FIVE) {
      expect(cell(field, { platform_push: ended }), `列 ${field} 不该紫`).not.toContain('#D15FE8')
    }
    // 前两列回到默认蓝；后三列回到「不加 style」。
    expect(cell('_orderid', { platform_push: ended })).toContain('#0094ff')
    expect(cell('orderId', { platform_push: ended })).toContain('#0094ff')
    expect(cell('_role', { platform_push: ended })).not.toContain('color:')
    expect(cell('_project', { platform_push: ended })).not.toContain('color:')
    expect(cell('_info', { platform_push: ended })).not.toContain('color:')
  })

  it('三角仍在（可查历史），但颜色跟着 `is_active`：活跃紫、全部结束默认蓝', () => {
    const active = cell('_orderid', { platform_push: push() })
    expect(active).toContain('tree_btn')
    expect(active).toContain(`color:${ORDER_NO_PUSHED_COLOR}`)
    const ended = cell('_orderid', { platform_push: { ...push(), is_active: false } })
    expect(ended).toContain('tree_btn')
    expect(ended).not.toContain(ORDER_NO_PUSHED_COLOR)
    expect(ended).toContain(`color:${ORDER_NO_DEFAULT_COLOR}`)
  })

  it('系统内置单号列：有推送紫 / 无推送蓝，且**值仍是 `order_no`**（不跟来源单号走）', () => {
    const pushed = cell('orderId', { platform_push: push(), source_order_no: 'TB-1' })
    expect(pushed).toContain('#D15FE8')
    expect(pushed).toContain('>20260921000000000001<')
    expect(pushed).not.toContain('TB-1')
    const plain = cell('orderId', { source_order_no: 'TB-1' })
    expect(plain).toContain('#0094ff')
    expect(plain).not.toContain('#D15FE8')
  })

  it('角色信息列：紫色只染文字，`[游戏名]区服` 与 escapeHtml 不变', () => {
    const html = cell('_info', { platform_push: push() })
    expect(html).toBe('<span style="color:#D15FE8">[魔兽世界]一区</span>')
  })
})

/**
 * 订单号左侧**三角展开**看该单绑定的平台单。
 *
 * ⚠️ 子行的**字段**取自我们自己的读面（`GET …/platform-push/`）：平台名 / 平台单号 /
 * 交易状态 / 发单价 / 操作（编辑）。**层级（父行下方、可多行）照对标，列集合按我们的读面**，
 * 已在文件头登记。
 */
describe('P6：订单号左侧三角 + 平台子订单子行（markup）', () => {
  const base = {
    order_no: '20260921000000000001',
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
    platform_push: null as PlatformPushSummary | null,
  }
  const cols = buildOrderCols(
    () => 'G',
    () => '',
  )
  const orderCell = (overrides: Partial<typeof base> = {}): string =>
    (cols[1]!.templet as (r: typeof base) => string)({ ...base, ...overrides })
  const push = (): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 12,
    remote_cancel_status: null,
    is_active: true,
  })
  const row = (overrides: Partial<DailiantongPushRow> = {}): DailiantongPushRow => ({
    id: 1,
    order: 1,
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    state_display: '已发出',
    price: '12.50',
    ensure1: '0.00',
    ensure2: '0.00',
    time_limit_hours: 720,
    ex_order_no: base.order_no,
    remote_status: 12,
    remote_cancel_status: 10,
    accept_uid: '',
    last_synced_at: null,
    last_error: '',
    attempts: 1,
    created_at: '2026-10-27T12:00:00+08:00',
    updated_at: '2026-10-27T12:00:00+08:00',
    ...overrides,
  })

  it('无推送 ⇒ **不画三角**（⛔ 不占位）', () => {
    expect(orderCell()).not.toContain('tree_btn')
    expect(orderCell()).not.toContain('layui-icon-triangle')
  })

  it('有推送 ⇒ 三角在订单号左侧，class / 图标 / 颜色 / 悬停逐字照对标 L1865', () => {
    const html = orderCell({ platform_push: push() })
    expect(html).toContain('tree_btn')
    expect(html).toContain('layui-icon-triangle-r')
    expect(html).toContain(`color:${ORDER_NO_PUSHED_COLOR}`)
    expect(html).toContain('title="点击可查看该订单下所有平台的子订单"')
    // 三角在徽标 / 订单号链接之前。
    expect(html.indexOf('tree_btn')).toBeLessThan(html.indexOf('layui-badge'))
    expect(html.indexOf('tree_btn')).toBeLessThan(html.indexOf('class="orderdetails"'))
  })

  /* ⭐ 展开区**不再是内嵌小表 + 自拟 5 列**，而是同一张表里的**真行**——列宽 / 表头 / 行高 /
   * 边框全部继承主表；只有三样是它自己的：订单号格 20px 缩进、子行白底 `#fff`、勾选格隐藏。
   * 平台侧取不到值的列一律「—」。 */
  it('B3：每一格按主表字段出内容——订单号 = 平台单号（缩进 20px / 紫）、状态 = 交易状态标签、发单价 = price、操作 = 编辑', () => {
    const noCell = platformChildCellHtml('_orderid', row(), base.order_no)
    expect(noCell).toContain('19000000000000000201')
    expect(noCell).toContain('margin-left:20px')
    expect(noCell).toContain('sontr')
    expect(noCell).toContain(`color:${ORDER_NO_PUSHED_COLOR}`)
    // 平台徽标与主表订单号列同一个出口（「通」那颗）
    expect(noCell).toContain('layui-badge')
    expect(noCell).toContain('>通<')

    const staCell = platformChildCellHtml('_sta', row(), base.order_no)
    expect(staCell).toContain('layui-badge')
    expect(staCell).toContain('正在代练')

    expect(platformChildCellHtml('_throw', row(), base.order_no)).toBe('12.50')

    const opCell = platformChildCellHtml('_change', row(), base.order_no)
    expect(opCell).toContain(PLATFORM_CHILD_EDIT_CLASS)
    expect(opCell).toContain('编辑')
    expect(opCell).toContain(`data-order="${base.order_no}"`)
  })

  it('B3：勾选格**空**（对标子行把勾选隐藏，L2267）；其余拿不到数据的列一律「—」', () => {
    expect(platformChildCellHtml('_orderId0', row(), base.order_no)).toBe('')
    for (const field of ['orderId', '_role', '_project', '_info', '_hour', '_ww', 'source_name',
      '_rec', '_price', '_netreceipts', '_refund', '_img', '_kefu', '_assUserName',
      '_create', '_completeTime', 'addOrderUserName', '_remark']) {
      expect(platformChildCellHtml(field, row(), base.order_no), `列 ${field}`).toBe(
        PLATFORM_CHILD_EMPTY_VALUE,
      )
    }
  })

  it('B3：上游文本过 escapeHtml（平台单号是上游数据，⛔ 直接拼 HTML）', () => {
    const cell = platformChildCellHtml('_orderid', row({ serial_no: '<b>&"' }), base.order_no)
    expect(cell).toContain('&lt;b&gt;&amp;&quot;')
    expect(cell).not.toContain('<b>')
  })

  it('B3：无平台单号 ⇒ 订单号格显示「—」（⛔ 不编一个假单号），徽标仍在', () => {
    const cell = platformChildCellHtml('_orderid', row({ serial_no: null, state: 'pending' }), base.order_no)
    expect(cell).toContain(PLATFORM_CHILD_EMPTY_VALUE)
    expect(cell).toContain('layui-badge')
  })

  it('B3：子行样式的取值逐条对得上对标（白底 / 20px 缩进；内嵌小表那三条自拟规则已删）', () => {
    expect(PLATFORM_CHILDREN_STYLE_RULES).toContain(`.${PLATFORM_CHILD_ROW_CLASS}{background-color:#fff}`)
    expect(PLATFORM_CHILDREN_STYLE_RULES).toContain('margin-left:20px')
    expect(PLATFORM_CHILD_INDENT_PX).toBe(20)
    // 旧的「一行跨列 + 内部小表」那套自拟规则必须整条删干净（否则又是两套视觉并存）
    expect(PLATFORM_CHILDREN_STYLE_RULES).not.toContain('tt-platform-children-table')
    expect(PLATFORM_CHILDREN_STYLE_RULES).not.toContain('tt-platform-children-cell')
    expect(PLATFORM_CHILDREN_STYLE_RULES).not.toContain('tt-platform-children-title')
  })
})

/**
 * 筛选栏第 2 行「代练打手」右边那一格（`#popgameName`「打手/发单人」）是**真控件**：
 * 真 `<select>`（`recType` 切档 + 关键词远程搜索），⛔ 不是「占位 div +
 * `data-notopen`」那种处置（D-0144）。
 * 候选的取数行为（远程搜索 / 零预载 / 切档重置）在 `order-orderlist-filters.spec.ts` 里测。
 */
describe('#popgameName：从占位 div 升级成真控件（T9 / spec §7.6）', () => {
  function host(): HTMLElement {
    const root = document.createElement('div')
    root.innerHTML =
      '<div class="layui-input-inline" style="width:140px">' +
      '<div id="popgameName" name="popgameName" class="xm-select-demo"></div>' +
      '</div>'
    return root
  }

  it('那个空 div 被换成带 `xm-select` 约定的真 `<select>`（单选 + 远程搜索）', () => {
    const root = host()
    renderPopgameName(root)
    const node = root.querySelector('#popgameName')
    expect(node).not.toBeNull()
    expect(node!.tagName).toBe('SELECT')
    expect(node!.getAttribute('name')).toBe('popgameName')
    expect(node!.getAttribute('xm-select')).toBe('popgameName')
    expect(node!.hasAttribute('xm-select-radio')).toBe(true)
    // ⭐ 结构性钉（2026-11-09，§4.3）：`xm-select-search` 的属性**值** = 端点 URL
    //（值就是 formselects 的 searchUrl；空值 = 本地搜索，那是改造前的形态）。
    expect(node!.getAttribute('xm-select-search')).toBe(FILTER_OPTIONS_SEARCH_URL)
    // 首项是占位 option（空关键词时下拉显示占位，这是「去预选」的自然结果）。
    const firstOption = node!.querySelector('option')
    expect(firstOption).not.toBeNull()
    expect(firstOption!.value).toBe('')
    expect(firstOption!.textContent).toBe(POPGAME_PLACEHOLDER)
    // ⛔ 占位处置的两个标志都不在：不再是 `xm-select` 折叠态自定义元素、也没有「尚未开放」的钩子。
    expect(node!.querySelector('xm-select')).toBeNull()
    expect(node!.getAttribute('data-notopen')).toBeNull()
    // 还留 `.xm-select-demo` 那个空 div 才算没换。
    expect(root.querySelector('div#popgameName')).toBeNull()
  })

  it('证据件里没有 #popgameName 时静默返回（不抛、不塞半截控件）', () => {
    const root = document.createElement('div')
    expect(() => renderPopgameName(root)).not.toThrow()
    expect(root.querySelector('#popgameName')).toBeNull()
  })
})

/**
 * ⭐ 2026-11-09（计划 §4.3）：打手 / 发单人两个下拉的**远程关键词搜索**——jsdom 里
 * formselects 未渲染，所以「配置对象怎么造」「信封怎么拆」抽成了可直调的纯函数
 * （页面既有测试同款手法），本 describe 直调它们钉**值**。
 */
describe('远程搜索：信封拆包与 formselects ajax 配置（纯函数直调）', () => {
  /** 合成信封（形状 = 后端 `filter_options` 视图的字面量）。 */
  const envelope = (data: unknown, code = 0) => ({ code, msg: code === 0 ? '' : '测试失败', data })
  const TAKERS = [
    { id: 11, name: '测试打手甲' },
    { id: 12, name: '测试打手乙' },
  ]
  const PUBLISHERS = [{ id: 21, name: '测试发单人甲' }]

  it('`popgameRowsFromEnvelope`：按响应时刻档位取对列表（`10` ⇒ publishers、其余 ⇒ takers）', () => {
    const res = envelope({ takers: TAKERS, publishers: PUBLISHERS })
    expect(popgameRowsFromEnvelope(res, '10')).toEqual(PUBLISHERS)
    expect(popgameRowsFromEnvelope(res, '0')).toEqual(TAKERS)
    // 判别力：两档取的是**不同的**列表（退化成恒取 takers / 恒取 publishers 的实现会红）。
    expect(popgameRowsFromEnvelope(res, '10')).not.toEqual(popgameRowsFromEnvelope(res, '0'))
  })

  it('信封 `code≠0` / `data` 缺失 / 行形状不合 ⇒ 空数组（控件显示「无匹配项」，不另造提示）', () => {
    const bad = envelope({ takers: TAKERS, publishers: PUBLISHERS }, 1002)
    expect(popgameRowsFromEnvelope(bad, '0')).toEqual([])
    expect(publisherRowsFromEnvelope(bad)).toEqual([])
    expect(popgameRowsFromEnvelope(envelope(null), '0')).toEqual([])
    expect(popgameRowsFromEnvelope(envelope({ takers: '不是数组' }), '0')).toEqual([])
    // 行形状不合（缺 id / id 非数字）的行被滤掉，合形的行保留。
    const mixed = envelope({ takers: [{ id: 'x', name: '坏行' }, { id: 13, name: '好行' }] })
    expect(popgameRowsFromEnvelope(mixed, '0')).toEqual([{ id: 13, name: '好行' }])
  })

  it('`publisherRowsFromEnvelope`：恒取 `publishers`（不受档位影响——那是 `#hallPublisherUser` 的口径）', () => {
    const res = envelope({ takers: TAKERS, publishers: PUBLISHERS })
    expect(publisherRowsFromEnvelope(res)).toEqual(PUBLISHERS)
    expect(publisherRowsFromEnvelope(res)).not.toEqual(TAKERS)
  })

  it('`buildFilterSearchData`：选了游戏发 `{game:<id>}`；没选 ⇒ 不带键（空串会被后端判 1000）', () => {
    expect(buildFilterSearchData(7)).toEqual({ game: 7 })
    expect(buildFilterSearchData(null)).toEqual({})
  })

  it('`buildFilterSearchConfig`：searchName=`search`、keyVal=`id`、delay=400、data 带当前游戏（钉值）', () => {
    const config = buildFilterSearchConfig({ game: 7, toRows: () => [] })
    expect(config.searchName).toBe('search')
    expect(config.keyVal).toBe('id')
    expect(config.delay).toBe(400)
    expect(config.data).toEqual({ game: 7 })
    // 没选游戏 ⇒ data 不带 `game` 键。
    expect(buildFilterSearchConfig({ game: null, toRows: () => [] }).data).toEqual({})
  })

  it('`buildFilterSearchConfig.beforeSuccess`：串**真** `popgameRowsFromEnvelope` 拆信封（档位 0 ⇒ takers）', () => {
    // ⭐ 不用桩：toRows 就是页面接线用的那个真拆包函数（判别力更强——桩写错形状照样绿，真函数不会）。
    const config = buildFilterSearchConfig({
      game: 7,
      toRows: (res) => popgameRowsFromEnvelope(res, '0'),
    })
    expect(
      config.beforeSuccess(
        'popgameName',
        FILTER_OPTIONS_SEARCH_URL,
        '关键词',
        envelope({ takers: [{ id: 11, name: '测试打手甲' }], publishers: [{ id: 21, name: '测试发单人甲' }] }),
      ),
    ).toEqual([{ id: 11, name: '测试打手甲' }])
  })

  it('`buildFilterSearchConfig.beforeSearch`：空 / 空白关键词 ⇒ false（不发请求）；非空 ⇒ true', () => {
    // ⭐ **为什么钉它**（jsdom 练不到 vendored formselects，行号就是那半边的证据）：
    // vendor 在**每次 render** 对设了 searchUrl 的控件自动触发一次「第一次请求」
    // （`formselects-v4.js:313-322`：`if (fs.config.searchUrl) { triggerSearch(reElem, true) }`，
    // 此时搜索框是空的；`:80` `first:true` 让这次首搜延迟只有 10ms），
    // 而 `beforeSearch` 是它**唯一的闸**（`:358`：返回假值 ⇒ search 分支短路、ajax 不发）。
    // 没有这条闸，挂载与每次切档位/切游戏的重渲染都会拉回前 200 全量名单——
    // 「去预选 / 空关键词不发请求」两条判据在真浏览器里被打回原形（页级桩防不住这一发）。
    const { beforeSearch } = buildFilterSearchConfig({ game: null, toRows: () => [] })
    expect(beforeSearch('popgameName', FILTER_OPTIONS_SEARCH_URL, '')).toBe(false)
    expect(beforeSearch('popgameName', FILTER_OPTIONS_SEARCH_URL, '   ')).toBe(false)
    expect(beforeSearch('popgameName', FILTER_OPTIONS_SEARCH_URL, 'sq')).toBe(true)
  })
})

/**
 * 行内「编辑订单」的**落地参数**。
 *
 * 为什么挂载整页：`openEditDialog` 是 `mountOrderListPage` 的**闭包**、没有导出，纯函数测不到它；
 * 只断言源码文本属于同义反复。这里把 layui 换成替身后 **spy `layer.open` 的入参**——
 * 既测了动作（点击第 28 列的 `.submitEdit`），也测了动作的落地参数。
 */
describe('行内「编辑订单」→ layer.open 的落地参数（type:2 iframe 大弹窗）', () => {
  const ROW = {
    order_no: '20260920213215233029',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-20T21:32:15+08:00',
  }

  beforeEach(() => {
    LAYUI.open.mockClear()
    LAYUI.msg.mockClear()
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
    document.body.innerHTML = ''
  })

  /** 挂载整页 → 用真实的第 28 列 templet 渲染出那颗「编辑」按钮 → 点它 → 返回 `layer.open` 的入参。 */
  async function clickEditButton(): Promise<Record<string, unknown>> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    await mountOrderListPage(root)

    const box = document.createElement('div')
    box.innerHTML = (
      buildOrderCols(
        () => 'G',
        () => '',
      )[22]!.templet as (d: typeof ROW) => string
    )(ROW)
    root.appendChild(box)
    const btn = box.querySelector('a.submitEdit')
    expect(btn).not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('type:2 / area 95%×95% / skin layui-layer-lan / title「<单号>编辑订单」/ anim 8 / shade', async () => {
    const opts = await clickEditButton()
    expect(opts.type).toBe(2)
    expect(opts.area).toEqual(['95%', '95%'])
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(8)
    expect(opts.title).toBe('20260920213215233029编辑订单')
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.shadeClose).toBe(true)
    expect(opts.maxmin).toBe(true)
  })

  it('iframe src 指向**我们的**录单页（`page=order-addorder` + `order_no`），不是对标的 `AddOrder?orderId=`', async () => {
    const opts = await clickEditButton()
    const src = String(opts.content)
    expect(src).toBe(
      '/layui-island/page.html?page=order-addorder&order_no=20260920213215233029',
    )
    expect(src).not.toContain('AddOrder?orderId=')
  })

  it('打开编辑弹窗的同一步上报 `order.open_edit`（spec §6.2 的前端上报腿；订单详情页加载**不**上报）', async () => {
    await clickEditButton()
    expect(vi.mocked(reportOrderOpenEditQuietly)).toHaveBeenCalledWith('20260920213215233029')
  })
})

/**
 * 「编辑保存成功」消息的落地 —— 本页是**父窗口**（编辑弹窗的宿主）。
 *
 * 发送方是 iframe 里的录单页编辑态（`order-addorder.ts` 的 `onSave` → `notifyOrderSaved`）：
 * 它 `PATCH /api/business/orders/<no>/` 成功之后 postMessage 回来，本页负责
 * **关弹窗 + 出提示 + 保留当前筛选与页码刷新列表**。
 *
 * 为什么断言必须打在**真实出口**上：`onOrderSavedMessage` 是 `mountOrderListPage` 的闭包、
 * 没有导出，只断言源码文本属于同义反复。这里挂载整页、点开编辑弹窗、再**真的派发一条
 * `MessageEvent`**，断言 `layer.close` / `table.reload` / `layer.msg` 三个真实调用与入参。
 *
 * ⚠️ **本组用例容忍前面 describe 留下的 window 监听器**（那些用例挂载后没调 cleanup）：
 * 所以断言用 `toHaveBeenCalledWith` 而不是 `toHaveBeenCalledTimes(1)`——被断言的是**我这一发
 * 消息引起的调用**，不是「全场只有一次调用」。负向用例不受影响（类型/来源不对，谁都不响应）。
 */
describe('编辑保存成功消息 → 关弹窗 + 保留筛选与页码刷新 + 成功提示', () => {
  const ROW = {
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

  beforeEach(() => {
    LAYUI.open.mockClear()
    // 真 layui 的 `layer.open` **返回层索引**（`layer.close` 要用它）；替身默认回 undefined，
    // 那样 `editLayerIndex` 落成 null、这条链就测不到「关的是哪一层」。
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.msg.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.tableHandlers.clear()
    LAYUI.form.val.mockReturnValue({})
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
    LAYUI.open.mockReset()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  /** 挂载整页 → 用真实的第 28 列 templet 点开「编辑」（会走 `openEditDialog`）→ 返回 cleanup。 */
  async function mountAndOpenEdit(): Promise<() => void> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    const box = document.createElement('div')
    box.innerHTML = (
      buildOrderCols(
        () => 'G',
        () => '',
      )[22]!.templet as (d: typeof ROW) => string
    )(ROW)
    root.appendChild(box)
    box
      .querySelector('a.submitEdit')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    return cleanup
  }

  function savedMessage(
    orderNo: string,
    origin = window.location.origin,
    msg = '',
  ): MessageEvent {
    return new MessageEvent('message', {
      data: { type: ORDER_SAVED_MESSAGE_TYPE, order_no: orderNo, msg },
      origin,
    })
  }

  it('收到保存成功消息：关闭编辑弹窗（用它自己的层索引）+ 出成功提示', async () => {
    const cleanup = await mountAndOpenEdit()
    // 前置哨兵：弹窗真的开了（否则「关了弹窗」可能由「压根没开」凑出来）
    expect(LAYUI.open).toHaveBeenCalledTimes(1)

    window.dispatchEvent(savedMessage(ROW.order_no))

    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
    // 空 `msg` ⇒ 回退默认那句（⛔ 不是显示一个空提示）。
    expect(LAYUI.msg).toHaveBeenCalledWith(EDIT_SAVE_SUCCESS_MESSAGE, { time: 1500 })
    cleanup()
  })

  /**
   * 编辑保存这条链**不许吞后端 `msg`**：后端保存成功后可能着手撤代练通订单；
   * 撤不掉时仍是成功码、警告只在 `msg` 里。
   */
  it('收到带后端 msg 的保存消息：原样显示那句（长停留 6000ms），⛔ 不再固定说「保存成功」', async () => {
    const cleanup = await mountAndOpenEdit()
    const NOTE = '已保存；这张单的代练通订单正在撤回中，请稍后刷新核对'
    window.dispatchEvent(savedMessage(ROW.order_no, window.location.origin, NOTE))
    expect(LAYUI.msg).toHaveBeenCalledWith(NOTE, { time: 6000 })
    expect(LAYUI.msg).not.toHaveBeenCalledWith(EDIT_SAVE_SUCCESS_MESSAGE, { time: 1500 })
    cleanup()
  })

  it('msg 含「未能撤回」警告 ⇒ 走 layer.alert 人工确认（⛔ 不一闪而过）', async () => {
    const cleanup = await mountAndOpenEdit()
    // `alert` 是跨用例共享的替身，且本文件有多个 describe 各自挂过页（消息监听器是**每实例**
    // 注册的）⇒ 这里判「最后一条 alert 的正文」而不是次数（次数会被测试环境里泄漏的旧实例
    // 放大；真浏览器里一页只有一个实例）。
    LAYUI.layer.alert.mockClear()
    const WARNING = '代练通订单未能撤回（原话：状态不允许），请先到『第三方平台订单管理』撤回'
    window.dispatchEvent(savedMessage(ROW.order_no, window.location.origin, WARNING))
    const alerts = LAYUI.layer.alert.mock.calls.map((call) => String(call[0]))
    expect(alerts.length).toBeGreaterThan(0)
    // 正文 = 后端 msg **原样**（alert 是「人工确认」那一档，不是一闪而过的 msg）
    expect(alerts[alerts.length - 1]).toBe(WARNING)
    expect(LAYUI.msg).not.toHaveBeenCalledWith(WARNING, { time: 1500 })
    cleanup()
  })

  it('刷新列表：带着当前筛选，且**保留 layui 交回的当前页码与每页条数**', async () => {
    const cleanup = await mountAndOpenEdit()
    // 用户先翻到第 3 页、再把「条/页」改成 50：
    //  - 页码走 `done(res, curr, count)`——layui **每次取数后**都会调它（vendored
    //    `public/layui/layui.js` L1 压缩单行、字符偏移 292034），`curr` 就是刚加载的那一页；
    //  - 「条/页」由 laypage 就地写进 `table.getOptions(id).page.limit`（同文件偏移 321568 / 35833）。
    // ⚠️ 原实现手调 `table.on('page(datatable)')` 那个**本版本不存在**的 handler（E-0037 的假绿形态）；
    //    现在驱动的是 `done` 这个真出口。
    LAYUI.table.getOptions.mockReturnValue({ page: { limit: 50 } })
    triggerDone({}, 3, 40)
    LAYUI.table.reload.mockClear()
    // 当前筛选：`form.val` 是筛选值的读取口，给一个真实条件，证明 `where` 不是空的。
    // ⚠️ 值必须是**证据件里那个中文档位**（`select[name=orderSta]` 的 option value），
    // 不是后端枚举名——`collectSearch()` 现在负责把前者映射成后者（R1）。
    LAYUI.form.val.mockReturnValue({ orderSta: '代练中' })

    window.dispatchEvent(savedMessage(ROW.order_no))

    expect(LAYUI.table.reload).toHaveBeenCalledWith(
      'datatable',
      expect.objectContaining({
        where: expect.objectContaining({ status: 'IN_PROGRESS' }),
        page: { curr: 3, limit: 50 },
      }),
    )
    cleanup()
  })

  it('发单页成功消息 ⇒ 关闭发单弹窗 + 刷新列表 + 提示「发单成功」', async () => {
    // 这一条要真的把**发单弹层**打开（层索引来自 `layer.open` 的返回值）⇒ 门必须放行。
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
        }
        if (String(url).includes(PUSH_AVAILABILITY_URL)) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ code: 0, data: { available: true } }),
          } as unknown as Response
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    // 用**真实的**「操作」列 templet 造出地球按钮（门开着才会画）。
    const renderCalls = LAYUI.table.render.mock.calls
    const opts = renderCalls[renderCalls.length - 1]?.[0] as {
      cols?: Array<Array<{ field?: string; templet?: unknown }>>
    }
    const changeCol = opts?.cols?.[0]?.find((col) => col.field === '_change')
    expect(changeCol?.templet).toBeTypeOf('function')
    const holder = document.createElement('div')
    holder.innerHTML = (changeCol!.templet as (d: unknown) => string)(ROW)
    root.appendChild(holder)
    LAYUI.open.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.msg.mockClear()

    holder
      .querySelector<HTMLElement>('a.pushJdpEdit')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    // 前置哨兵：弹层真的开了、拿到了层索引 7。
    expect(LAYUI.open).toHaveBeenCalledTimes(1)

    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: PUSH_JDP_SENT_MESSAGE_TYPE, order_no: ROW.order_no },
        origin: window.location.origin,
      }),
    )

    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
    expect(LAYUI.table.reload).toHaveBeenCalledWith('datatable', expect.anything())
    expect(LAYUI.msg).toHaveBeenCalledWith(PUSH_JDP_SENT_TEXT, { time: 1500 })
    cleanup()
  })

  it('类型不对 / 跨源的消息一律不响应（不关弹窗、不提示、不刷新）', async () => {
    const cleanup = await mountAndOpenEdit()
    const msgBefore = LAYUI.msg.mock.calls.length
    const reloadBefore = LAYUI.table.reload.mock.calls.length
    // ① 同源但类型不对（模拟 `island:navigate` 那种别的消息）
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'island:navigate', name: 'order-detail' },
        origin: window.location.origin,
      }),
    )
    // ② 类型对、但 origin 不是本页来源
    window.dispatchEvent(savedMessage(ROW.order_no, 'http://evil.example'))

    expect(LAYUI.msg.mock.calls.length).toBe(msgBefore)
    expect(LAYUI.table.reload.mock.calls.length).toBe(reloadBefore)
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    cleanup()
  })
})

/**
 * 「更改订单状态」弹窗（业务方裁定）。
 *
 * 这里**只钉值**（不是钉常量）：期望值全部写成字面量，源码里的常量被改坏时这些用例会红。
 * 口径：① 弹窗里**没有**「当前状态：」行；② 选项取 `allowed_transitions`（不铺全 13 个）；
 * ③ `REFUNDED` 不特判——后端给它加了回池出边，它有 2 个可改目标，弹窗照常打开。
 */
describe('「更改订单状态」弹窗 = 对标参数 + 选项取 allowed_transitions（无「当前状态」行、无预选）', () => {
  const ROW = {
    order_no: '20260920213215233029',
    source_order_no: '',
    status: 'IN_PROGRESS',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-20T21:32:15+08:00',
  }
  /** 详情接口回的订单（`IN_PROGRESS` 的真实可用出边，顺序取后端枚举序）。 */
  const ORDER = {
    order_no: ROW.order_no,
    status: 'IN_PROGRESS',
    allowed_transitions: ['ISSUE', 'PAUSED', 'PENDING_REVIEW', 'REFUNDED', 'UNASSIGNED'],
  }

  beforeEach(() => {
    LAYUI.open.mockClear()
    LAYUI.open.mockImplementation(() => 7)
    LAYUI.msg.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.layer.alert.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.form.render.mockClear()
    LAYUI.form.val.mockReturnValue({})
    vi.mocked(fetchOrder).mockReset()
    vi.mocked(transitionOrder).mockReset()
    vi.mocked(fetchOrder).mockResolvedValue(ORDER as never)
    vi.mocked(transitionOrder).mockResolvedValue(undefined as never)
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
    LAYUI.open.mockReset()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  /**
   * 挂载整页 → 点第 10 列（状态）徽标 → 让 `fetchOrder` 那一跳的微任务全部走完。
   *
   * 不用 `vi.waitFor(layer.open 被调用)`：终态那两条用例里 `layer.open` **本来就不该**被调用，
   * 等它只会白等一个超时，还会把「没开弹窗」与「超时」混成同一件事。
   * 一个宏任务足以排空已 resolve 的 promise 链（`fetchOrder` 是 `mockResolvedValue`）。
   */
  async function openDialog(
    rowOverrides: Partial<typeof ROW> & { platform_push?: PlatformPushSummary | null } = {},
  ): Promise<() => void> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    await mountOrderListPage(root)

    const row = { ...ROW, ...rowOverrides }
    const box = document.createElement('div')
    box.innerHTML = (
      buildOrderCols(
        () => 'G',
        () => '',
      )[6]!.templet as (d: typeof ROW) => string
    )(row)
    root.appendChild(box)
    const badge = box.querySelector('.setdlsta')
    expect(badge).not.toBeNull()
    badge!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    return () => {
      document.body.innerHTML = ''
    }
  }

  function openedOpts(): Record<string, unknown> {
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('layer 参数逐项照对标 L3802-3813：title/area/skin/anim/shade/shadeClose/type', async () => {
    const cleanup = await openDialog()
    const opts = openedOpts()
    expect(opts.title).toBe('更改订单状态')
    expect(opts.area).toEqual(['1000px', '300px'])
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(5)
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.shadeClose).toBe(true)
    expect(opts.type).toBe(1)
    cleanup()
  })

  it('**没有** `btn`（对标只有标题栏 X / 点外面关 ⇒ 保存按钮在内容里，不是 layer 的按钮）', async () => {
    const cleanup = await openDialog()
    const opts = openedOpts()
    expect('btn' in opts).toBe(false)
    expect(opts.yes).toBeUndefined()
    cleanup()
  })

  it('内容标记照对标子页：`.layui-form.layui-form-pane` + 110px label + `name="status"` + savebtn + `font-size:12px`', async () => {
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    expect(body).toContain('class="layui-form layui-form-pane"')
    expect(body).toContain('padding-top:20px;margin-left:10px;margin-right:10px;font-size:12px')
    expect(body).toContain('<label class="layui-form-label">要改成：</label>')
    expect(body).toContain('class="layui-input-block"')
    expect(body).toContain('name="status"')
    expect(body).toContain('class="layui-btn layui-btn-normal savebtn"')
    expect(body).toContain('data-act="status-save"')
    // 对标按钮里的 `&#x1005;` 是它的笔误（缅文字母），按「不复刻对标缺陷」换成对勾 `&#xe605;`
    expect(body).toContain('&#xe605;')
    expect(body).not.toContain('&#x1005;')
    // 对标 13 个 radio 一个 checked 都没有 ⇒ 我们也不预选（避免第一个选项被误读成当前状态）
    expect(body).not.toContain('checked')
    cleanup()
  })

  it('弹窗里**没有**「当前状态：」那一行（业务方 2026-09-21 裁定第 1 条，与对标一致）', async () => {
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    // ⭐ D-0337 起弹窗有两个可见 label（「要改成：」+「说明原因：」，见下面那条用例钉
    //    「恰好 2 个」）；本条只钉「没有当前状态」。
    expect(body).not.toContain('当前状态')
    expect(body).not.toContain('data-status=')
    // 阳性哨兵：弹窗**确实**开了、且确实有「要改成：」那一行。
    // 否则「没有当前状态」在一个根本没渲染出内容的弹窗上也会通过（空内容恒真）。
    expect(LAYUI.open).toHaveBeenCalled()
    expect(body).toContain('<label class="layui-form-label">要改成：</label>')
    cleanup()
  })

  it('选项文案走本页 `statusLabel`：目标 `WAITING_GRAB` 显示「待抢单」（与后端/大厅页一致）', async () => {
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'ISSUE',
      allowed_transitions: ['WAITING_GRAB', 'UNASSIGNED'],
    } as never)
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    expect(body).toContain('value="WAITING_GRAB" title="待抢单"')
    expect(body).not.toContain('title="待上号"')
    cleanup()
  })

  it('选项 = `allowed_transitions`（文案 `statusLabel`），且只有这几个', async () => {
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    for (const [t, label] of [
      ['ISSUE', '问题单'],
      ['PAUSED', '已暂停'],
      ['PENDING_REVIEW', '待验收'],
      ['REFUNDED', '已退单'],
      ['UNASSIGNED', '未分配'],
    ] as const) {
      expect(body).toContain(`value="${t}" title="${label}"`)
    }
    // 不在 `allowed_transitions` 里的全都不出（`COMPLETED` 要节点 17，见 Task 4 的两条用例）
    expect(body).not.toContain('value="COMPLETED"')
    expect(body).not.toContain('value="WAITING_GRAB"')
    cleanup()
  })

  it('Task 4：后端收紧后的集合——有节点的目标出按钮，无节点的（无 17 时的 COMPLETED）不出', async () => {
    // `OrderSerializer.get_allowed_transitions` 的契约是「**通用 `/transition/`** 会接受的
    // 目标集合」：不持 `order.change_status_completed`（17）的调用方拿不到 `COMPLETED`。
    // 前端只按这个契约渲染，⛔ 不得自行镜像白名单补出「已完成」。
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'PENDING_REVIEW',
      allowed_transitions: ['IN_PROGRESS', 'ISSUE', 'PAUSED', 'REFUNDED'],
    } as never)
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    for (const [t, label] of [
      ['IN_PROGRESS', '代练中'],
      ['ISSUE', '问题单'],
      ['PAUSED', '已暂停'],
      ['REFUNDED', '已退单'],
    ] as const) {
      expect(body).toContain(`value="${t}" title="${label}"`)
    }
    expect(body.match(/name="status"/g)).toHaveLength(4)
    expect(body).not.toContain('value="COMPLETED"')
    cleanup()
  })

  it('Task 4：集合里**有** `COMPLETED`（调用方持 17）⇒ 它必须渲染出按钮（正面哨兵）', async () => {
    // 反面用了「无 17 ⇒ 没有 COMPLETED」，这一条保证前端不会把「已完成」**无条件**藏掉
    // （否则把两个方向合成一个恒真的「不出」判据）。
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'PENDING_REVIEW',
      allowed_transitions: ['COMPLETED'],
    } as never)
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    expect(body).toContain('value="COMPLETED" title="已完成"')
    expect(body.match(/name="status"/g)).toHaveLength(1)
    cleanup()
  })

  it('`success` 里 `form.render(\'radio\')`（不渲染 ⇒ 用户看到没文字的圆圈）', async () => {
    const cleanup = await openDialog()
    const success = openedOpts().success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.innerHTML = String(openedOpts().content)
    dialog.id = 'tt-orderlist-transition'
    success({ find: () => ({ 0: dialog }) }, 7)
    expect(LAYUI.form.render).toHaveBeenCalledWith('radio')
    cleanup()
  })

  it('点「确定」未选状态：照对标 `setordersta.js` L42 提示，且**不**发请求、**不**关弹窗', async () => {
    const cleanup = await openDialog()
    const success = openedOpts().success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.id = 'tt-orderlist-transition'
    dialog.innerHTML = String(openedOpts().content)
    success({ find: () => ({ 0: dialog }) }, 7)

    dialog.querySelector('[data-act="status-save"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).toHaveBeenCalledWith('请选择要改变的状态', { time: 1500 })
    expect(transitionOrder).not.toHaveBeenCalled()
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    cleanup()
  })

  it('点「确定」已选状态：关本层 → 提交 → **先 reload 再报成功**（顺序反了成功提示会被警告顶掉）', async () => {
    const cleanup = await openDialog()
    const success = openedOpts().success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.id = 'tt-orderlist-transition'
    dialog.innerHTML = String(openedOpts().content)
    success({ find: () => ({ 0: dialog }) }, 7)
    const picked = dialog.querySelector<HTMLInputElement>('input[value="PENDING_REVIEW"]')!
    picked.checked = true

    dialog.querySelector('[data-act="status-save"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('状态已更新', { time: 1500 }))

    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
    expect(transitionOrder).toHaveBeenCalledWith(ROW.order_no, { target: 'PENDING_REVIEW' })
    // 顺序判据：`reload()`（= `table.reload`）必须先于「状态已更新」那句 msg
    const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
    const okMsgOrder = LAYUI.msg.mock.calls.findIndex((c) => c[0] === '状态已更新')
    expect(reloadOrder).toBeLessThan(LAYUI.msg.mock.invocationCallOrder[okMsgOrder]!)
    cleanup()
  })

  it('已退单：照常开弹窗，选项恰好 2 个（未分配 / 待抢单）——**不再**按终态拦住', async () => {
    // ⚠️ `REFUNDED` 不特判：后端 `order_status.py` 给它两条回池出边 ⇒ 详情接口回这两个目标。
    // 这里断言的三件事：弹窗**开了**、选项**恰好这两条**、且**没有**那句终态提示。
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'REFUNDED',
      allowed_transitions: ['UNASSIGNED', 'WAITING_GRAB'],
    } as never)
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    expect(LAYUI.open).toHaveBeenCalled()
    expect(body.match(/name="status"/g)).toHaveLength(2)
    expect(body).toContain('value="UNASSIGNED" title="未分配"')
    expect(body).toContain('value="WAITING_GRAB" title="待抢单"')
    expect(LAYUI.msg).not.toHaveBeenCalledWith('「已退单」是终态，不能再更改状态', { time: 1500 })
    cleanup()
  })

  it('拿到空数组（非 REFUNDED 特判）：按**没有可执行的目标**提示', async () => {
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'COMPLETED',
      allowed_transitions: [],
    } as never)
    const cleanup = await openDialog()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('当前账号没有可执行的流转（职位权限可能不足）', { time: 1500 })
    cleanup()
  })

  /* ── ⭐ D-0337：选问题单 / 已暂停 / 已退单 ⇒ 「说明原因」行 + 必填 ──────
   * 三档（对标还有「异常中」，我们没有）显示 `.remark` 行并 focus，别的档收起；
   * 行标记（label / placeholder / 类名）照子页原文。
   */

  /** 开弹窗并把内容挂成弹层 DOM、执行 `success`（照上面各交互用例的同一套形状）。 */
  async function openMountedDialog(): Promise<{ dialog: HTMLElement; cleanup: () => void }> {
    const cleanup = await openDialog()
    const success = openedOpts().success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.id = 'tt-orderlist-transition'
    dialog.innerHTML = String(openedOpts().content)
    success({ find: () => ({ 0: dialog }) }, 7)
    return { dialog, cleanup }
  }

  it('说明原因行照对标子页原文：`.remark` 默认 display:none + label「说明原因：」+ placeholder + maxlength=200', async () => {
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    // label 文案 / placeholder / 类名 / autocomplete 照子页原文；
    // `maxlength` 按业务方口径 200（对标是 50——差异登记在 D-0337）。
    expect(body).toContain('<div class="layui-form-item remark" style="display:none">')
    expect(body).toContain('<label class="layui-form-label">说明原因：</label>')
    expect(body).toContain('name="remark"')
    expect(body).toContain('id="remark"')
    expect(body).toContain('autocomplete="off"')
    expect(body).toContain('placeholder="请输入原因"')
    expect(body).toContain('class="layui-input layui-input-shadow"')
    expect(body).toContain('maxlength="200"')
    // 对标子页的「同步淘宝发货」行**不**复刻（业务方只要说明原因行）
    expect(body).not.toContain('同步淘宝发货')
    cleanup()
  })

  it('弹窗 label 恰好两个：「要改成：」与「说明原因：」（D-0337 起与对标 DOM 同形）', async () => {
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    expect(body).toContain('<label class="layui-form-label">要改成：</label>')
    expect(body.match(/<label class="layui-form-label">/g)).toHaveLength(2)
    cleanup()
  })

  it('选「问题单」⇒ 说明原因行显示并 focus；换选「待验收」⇒ 收起（显隐照对标 .show()/.hide()，不清空已填值）', async () => {
    const { dialog, cleanup } = await openMountedDialog()
    const row = dialog.querySelector<HTMLElement>('.remark')!
    const input = dialog.querySelector<HTMLInputElement>('#remark')!
    expect(row.style.display).toBe('none')
    // ⚠️ focus 用 spy 断言：dialog 不在 document 树上（既有弹层用例的同一形状），jsdom 对
    //    未挂树的元素不更新 `activeElement` ⇒ 判据钉「实现调用了 focus」，不打在 activeElement 上。
    const focusSpy = vi.spyOn(input, 'focus')

    const issue = dialog.querySelector<HTMLInputElement>('input[value="ISSUE"]')!
    issue.checked = true
    issue.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(row.style.display).toBe('')
    expect(focusSpy).toHaveBeenCalled()

    // 填一个值再换档：收起**不清空**值 ⇒ 再切回来时值还在。
    input.value = '打手联系不上'
    const review = dialog.querySelector<HTMLInputElement>('input[value="PENDING_REVIEW"]')!
    issue.checked = false
    review.checked = true
    review.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(row.style.display).toBe('none')
    expect(input.value).toBe('打手联系不上')
    cleanup()
  })

  it.each(['ISSUE', 'PAUSED', 'REFUNDED'] as const)(
    '选 %s 未填原因点确定 ⇒ 拦「请输入原因」、零请求、不关层',
    async (status) => {
      const { dialog, cleanup } = await openMountedDialog()
      dialog.querySelector<HTMLInputElement>(`input[value="${status}"]`)!.checked = true
      dialog
        .querySelector('[data-act="status-save"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入原因', { time: 1500 })
      expect(transitionOrder).not.toHaveBeenCalled()
      expect(LAYUI.layer.close).not.toHaveBeenCalledWith(7)
      cleanup()
    },
  )

  it('选「问题单」只填空白 ⇒ 同样拦下（trim 判空，与后端口径一致）', async () => {
    const { dialog, cleanup } = await openMountedDialog()
    dialog.querySelector<HTMLInputElement>('input[value="PAUSED"]')!.checked = true
    ;(dialog.querySelector('#remark') as HTMLInputElement).value = '   '
    dialog
      .querySelector('[data-act="status-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.msg).toHaveBeenCalledWith('请输入原因', { time: 1500 })
    expect(transitionOrder).not.toHaveBeenCalled()
    cleanup()
  })

  it('选「问题单」填了原因点确定 ⇒ 载荷带 trim 后的 reason；层关、成功提示照旧', async () => {
    const { dialog, cleanup } = await openMountedDialog()
    dialog.querySelector<HTMLInputElement>('input[value="ISSUE"]')!.checked = true
    ;(dialog.querySelector('#remark') as HTMLInputElement).value = '  客户反馈技能不达标  '
    dialog
      .querySelector('[data-act="status-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('状态已更新', { time: 1500 }))
    expect(transitionOrder).toHaveBeenCalledWith(ROW.order_no, {
      target: 'ISSUE',
      reason: '客户反馈技能不达标',
    })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
    cleanup()
  })

  it.each(['PENDING_REVIEW', 'UNASSIGNED'] as const)(
    '回归钉：选 %s（非三状态）点确定 ⇒ 载荷**不带** reason 键（行里的值也不发出去）',
    async (status) => {
      const { dialog, cleanup } = await openMountedDialog()
      dialog.querySelector<HTMLInputElement>(`input[value="${status}"]`)!.checked = true
      ;(dialog.querySelector('#remark') as HTMLInputElement).value = '不该被发出去的原因'
      dialog
        .querySelector('[data-act="status-save"]')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await vi.waitFor(() => expect(transitionOrder).toHaveBeenCalled())
      const payload = vi.mocked(transitionOrder).mock.calls[0]![1]
      expect('reason' in payload).toBe(false)
      cleanup()
    },
  )

  /* ── ⭐ 已抛到代练通的单不能手工改状态（入口整体不可用） ───────────── */

  /** 行数据里的有效推送摘要（判据只看它，⛔ 不看状态名）。 */
  const PUSH = {
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent' as const,
    remote_status: 11,
    remote_cancel_status: 10,
    is_active: true,
  }

  it('有活跃平台订单（`is_active=true`）⇒ 点状态徽标只弹提示：**不开弹窗、零取数、零提交**', async () => {
    const cleanup = await openDialog({ platform_push: PUSH })
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(fetchOrder).not.toHaveBeenCalled()
    expect(transitionOrder).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '这张单已抛到代练通，状态由平台决定并自动同步，不能手工修改',
      { time: 1500 },
    )
    cleanup()
  })

  it('提示文案由常量给出（业务方原话，⛔ 别改词）', () => {
    expect(PLATFORM_STATUS_NOTICE).toBe(
      '这张单已抛到代练通，状态由平台决定并自动同步，不能手工修改',
    )
  })

  it('平台订单全部结束（`is_active=false`）⇒ 恢复普通内部订单：**正常开弹窗、全部可选**', async () => {
    const cleanup = await openDialog({
      platform_push: { ...PUSH, remote_status: 17, is_active: false },
    })
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(fetchOrder).toHaveBeenCalled()
    expect(LAYUI.msg).not.toHaveBeenCalledWith(PLATFORM_STATUS_NOTICE, { time: 1500 })
    expect(String(openedOpts().content)).toContain('name="status"')
    cleanup()
  })

  it('判据是**行数据**而不是状态名：IN_PROGRESS 但无 platform_push ⇒ 照常开弹窗、全部可选', async () => {
    // ROW.status 就是 IN_PROGRESS；没有 platform_push ⇒ 旧行为（弹窗 + 全部可选）。
    const cleanup = await openDialog()
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(LAYUI.msg).not.toHaveBeenCalledWith(PLATFORM_STATUS_NOTICE, { time: 1500 })
    const body = String(openedOpts().content)
    expect(body.match(/name="status"/g)).toHaveLength(5)
    expect(body).not.toContain('disabled')
    cleanup()
  })

  it('状态徽标只在**有活跃平台订单**时带 `data-push="Y"`（无推送 / 全部结束都不带）', () => {
    const render = (d: typeof ROW & { platform_push?: PlatformPushSummary | null }): string =>
      (
        buildOrderCols(
          () => 'G',
          () => '',
        )[6]!.templet as (r: typeof ROW) => string
      )(d)
    expect(render({ ...ROW, platform_push: PUSH })).toContain('data-push="Y"')
    expect(render({ ...ROW, platform_push: { ...PUSH, is_active: false } })).not.toContain(
      'data-push',
    )
    expect(render(ROW)).not.toContain('data-push')
  })

  it('后端 A2 兜底：提交被拒 ⇒ **原样显示后端中文**，⛔ 不报「状态已更新」', async () => {
    vi.mocked(transitionOrder).mockRejectedValue(
      new BizError(1002, '已抛到平台的订单不能手工改状态（后端原文）'),
    )
    const cleanup = await openDialog()
    const success = openedOpts().success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.id = 'tt-orderlist-transition'
    dialog.innerHTML = String(openedOpts().content)
    success({ find: () => ({ 0: dialog }) }, 7)
    dialog.querySelector<HTMLInputElement>('input[value="PENDING_REVIEW"]')!.checked = true
    dialog
      .querySelector('[data-act="status-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('已抛到平台的订单不能手工改状态（后端原文）', {
        time: 1500,
      }),
    )
    expect(transitionOrder).toHaveBeenCalledWith(ROW.order_no, { target: 'PENDING_REVIEW' })
    expect(LAYUI.msg).not.toHaveBeenCalledWith('状态已更新', { time: 1500 })
    cleanup()
  })

  /* ── F3（spec §2.3）：下拉里「已完成」照旧有；不是待验收时选中它只弹提示、不发请求 ── */

  /**
   * `openDialog()` 之后把内容挂成弹层 DOM 并选中「已完成」那一档，再点「确定」。
   * 返回弹层节点与 cleanup（用例自己要断言提示窗 / 请求面）。
   */
  async function pickCompletedAndSave(): Promise<{ dialog: HTMLElement; cleanup: () => void }> {
    const cleanup = await openDialog()
    const success = openedOpts().success as (l: unknown, i: number) => void
    const dialog = document.createElement('div')
    dialog.id = 'tt-orderlist-transition'
    dialog.innerHTML = String(openedOpts().content)
    success({ find: () => ({ 0: dialog }) }, 7)
    const picked = dialog.querySelector<HTMLInputElement>('input[value="COMPLETED"]')
    expect(picked, '选项里没有「已完成」').not.toBeNull()
    picked!.checked = true
    dialog
      .querySelector('[data-act="status-save"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    return { dialog, cleanup }
  }

  it('F3-U5：选「已完成」且 `completed_requires_review=true` ⇒ 提示窗文案逐字、零请求', async () => {
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'IN_PROGRESS',
      allowed_transitions: ['PENDING_REVIEW', 'COMPLETED'],
      completed_requires_review: true,
    } as never)
    const { cleanup } = await pickCompletedAndSave()
    // 文案**逐字**（后端常量同一句；只判第一参，不锁第二参的 layui 选项对象）
    expect(LAYUI.layer.alert.mock.calls.map((c) => c[0])).toContain(
      '必须先把订单状态改成待验收才能改成已完成',
    )
    // 零请求：`/transition/` 一次都没打，弹层也**不关**（用户还能改选别的目标）
    expect(transitionOrder).not.toHaveBeenCalled()
    expect(LAYUI.layer.close).not.toHaveBeenCalledWith(7)
    cleanup()
  })

  it('F3-U6：`completed_requires_review=false`（待验收）⇒ 照旧提交，行为不变', async () => {
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'PENDING_REVIEW',
      allowed_transitions: ['COMPLETED'],
      completed_requires_review: false,
    } as never)
    const { cleanup } = await pickCompletedAndSave()
    await vi.waitFor(() =>
      expect(transitionOrder).toHaveBeenCalledWith(ROW.order_no, { target: 'COMPLETED' }),
    )
    // 反向哨兵：可正常改完成的那一档**不该**被一刀切拦下
    expect(LAYUI.layer.alert).not.toHaveBeenCalled()
    cleanup()
  })

  it('F3-U7：`allowed_transitions` 里的「已完成」**照旧出现在选项里**（不是把它从下拉删掉）', async () => {
    vi.mocked(fetchOrder).mockResolvedValue({
      order_no: ROW.order_no,
      status: 'IN_PROGRESS',
      allowed_transitions: ['PENDING_REVIEW', 'COMPLETED'],
      completed_requires_review: true,
    } as never)
    const cleanup = await openDialog()
    const body = String(openedOpts().content)
    expect(body).toContain('value="COMPLETED" title="已完成"')
    expect(body.match(/name="status"/g)).toHaveLength(2)
    cleanup()
  })
})

/* ═══════════ 发到内部接单大厅（发价弹层）/ 从内部接单大厅撤回 ═══════════ */

/**
 * 一行真实形状的订单载荷（字段集 = `OrderListSerializer`）。
 * 相关用例跨多个 describe，所以这一份放在**模块作用域**——各 describe 里那些局部 `ROW`
 * 在作用域外不可见（照抄它们会得到一份各自漂移的副本）。
 */
const ROW = {
  order_no: '20260920213215233029',
  source_order_no: '',
  status: 'UNASSIGNED',
  game: 2,
  server: null,
  project_text: 'x',
  total_price: '1.00',
  // 列表载荷的 `refund_fee` 是**必填**键（spec §5.3 / C6），夹具按真实契约带上它。
  refund_fee: '0.00',
  customer_phone: '',
  is_urgent: false,
  created_at: '2026-09-20T21:32:15+08:00',
}

/** 池内状态那一行（「已抛单、还没人接」）。 */
const WAITING_ROW = { ...ROW, status: 'WAITING_GRAB' }

/** 末列「操作」的**真实 templet**（页面的 `btnfun`）——不手抄标记，避免与实现分叉。 */
function renderActionCell(row: typeof ROW): string {
  return (buildOrderCols(() => 'G', () => '')[22]!.templet as (d: typeof ROW) => string)(row)
}

/** 点发价弹层里的「保存」（面板可能已被 layui 搬进 `.layui-layer-content`）。 */
function clickThrowSave(panel: HTMLElement): void {
  panel
    .querySelector('[lay-filter="saveSettpricebtn"]')!
    .dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

describe('Task 4 导出的常量钉回对标字面值（E-0024：期望值写字面值，不拿常量断言常量）', () => {
  it('THROW_DIALOG_TITLE = orderlist.js:3856 的原文', () => {
    expect(THROW_DIALOG_TITLE).toBe('设定抛单价及保证金')
  })

  it('WITHDRAW_CONFIRM_TEXT = orderlist.js:3957 的原文', () => {
    expect(WITHDRAW_CONFIRM_TEXT).toBe(
      '确定从内部接单大厅撤销该订单吗？撤销后，内部接单大厅将看不到该订单',
    )
  })
})

describe('第 28 列第 2 颗按钮按状态切换：WAITING_GRAB → 「从内部接单大厅撤回」', () => {
  it('class/图标/`lay-tips` 逐字取对标（orderlist.js:2155）', () => {
    const html = renderActionCell(WAITING_ROW)
    expect(html).toContain('layui-btn layui-btn-xs layui-btn-warm cancelThrowHome')
    expect(html).toContain('layui-icon-prev')
    expect(html).toContain('lay-tips="从内部接单大厅撤回"')
    expect(html).not.toContain('throwEdit')
  })

  it('非池内状态（UNASSIGNED）仍是 `throwEdit`「发到内部接单大厅」（既有 4 颗不回归）', () => {
    const html = renderActionCell(ROW)
    expect(html).toContain('layui-btn layui-btn-xs layui-btn-warm throwEdit')
    expect(html).toContain('lay-tips="发到内部接单大厅"')
    expect(html).not.toContain('cancelThrowHome')
  })

  it('撤回那颗的 `style` 只有 `margin-bottom`（对标 2155 原文，没有 `margin-right`）', () => {
    const html = renderActionCell(WAITING_ROW)
    expect(html).toContain('cancelThrowHome" style="margin-bottom: 0px;"')
    expect(html).not.toContain('margin-right: 0px;margin-bottom: 0px;" lay-tips="从内部接单大厅撤回"')
  })

  it('`data-id` / `data-viewId` 都是订单号（页内委托读 `dataset.id`）', () => {
    const html = renderActionCell(WAITING_ROW)
    // G2 起短信那颗已删；WAITING_GRAB 行只有「编辑 / 撤回 / 发平台」三颗。
    expect([...html.matchAll(/data-id="([^"]+)"/g)].map((m) => m[1])).toEqual([
      ROW.order_no,
      ROW.order_no,
      ROW.order_no,
    ])
    expect(html).toContain(`data-id="${ROW.order_no}" data-viewId="${ROW.order_no}"`)
  })
})

describe('证据件：发价保存按钮上的「尚未开放」标注已摘掉（它现在真接上了）', () => {
  it('`public/replica/order-orderlist.html` 里不再有 data-notopen="设定抛单价及保证金"', () => {
    expect(EVIDENCE).not.toContain('data-notopen="设定抛单价及保证金"')
    // 但保存按钮本身还在（不是把整颗按钮删了）
    expect(EVIDENCE).toContain('lay-filter="saveSettpricebtn"')
  })
})

describe('管理订单页「发内部 / 撤内部」的落地参数（挂载整页 + 真 templet 节点 + 真实调用）', () => {
  beforeEach(() => {
    LAYUI.open.mockReset()
    LAYUI.msg.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.layer.confirm.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.table.checkStatus.mockReturnValue({ data: [] })
    LAYUI.form.render.mockClear()
    LAYUI.form.val.mockReturnValue({})
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    vi.mocked(throwToHall).mockReset()
    vi.mocked(withdrawThrow).mockReset()
    // 默认给一个**已兑现**的返回值（同 Task 3 的 `claimOrder` 桩）：`mockReset()` 后是 `undefined`，
    // 只点按钮不断言结果的用例会在 `.then` 上抛 `TypeError`（那测的是替身的形状，不是页面行为）。
    vi.mocked(throwToHall).mockResolvedValue({} as never)
    vi.mocked(withdrawThrow).mockResolvedValue({} as never)
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
    document.body.innerHTML = ''
  })

  /** 挂载整页（证据件走全局 fetch；字典走 `@/api/config` 的替身）。 */
  async function mountOrderList(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    return { root, cleanup }
  }

  /**
   * 挂载整页 → 用**真实 templet**（`btnfun`）造一颗按钮塞进 root → 点它。
   * 这样断言的是「页面渲染出来的那颗按钮被点之后发生了什么」，而不是测试自己拼的标记。
   */
  async function mountAndClickAction(
    cls: 'throwEdit' | 'cancelThrowHome',
    row: typeof ROW,
  ): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const { root, cleanup } = await mountOrderList()
    const box = document.createElement('div')
    box.innerHTML = renderActionCell(row)
    root.appendChild(box)
    const btn = box.querySelector(`a.${cls}`)
    expect(btn, `第 28 列没渲染出 a.${cls}`).not.toBeNull()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    return { root, cleanup }
  }

  describe('行内「发到内部接单大厅」→ 发价弹层（对标 orderlist.js:3841-3882）', () => {
    it('layer.open 的 type/title/skin/anim/area/shade/shadeClose 逐项等于对标，content 来自 .setprice', async () => {
      LAYUI.open.mockImplementation(() => 7)
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
      expect(opts.type).toBe(1)
      // 期望值写字面值（E-0024）：常量单独有用例钉字面值，这里再钉一次落地参数
      expect(opts.title).toBe('设定抛单价及保证金')
      expect(opts.title).toBe(THROW_DIALOG_TITLE)
      expect(opts.skin).toBe('layui-layer-lan')
      expect(opts.anim).toBe(5)
      expect(opts.area).toEqual(['510px', '350px'])
      expect(opts.shade).toEqual([0.3, '#333'])
      expect(opts.shadeClose).toBe(true)
      // content 是**页内那个隐藏容器**（`layui.$` 包过的 jQuery 对象），不是自造的标记
      const panel = root.querySelector('.setprice')
      expect(panel).not.toBeNull()
      expect(opts.content).toBe(panel)
      cleanup()
    })

    it('每次打开都复位：#setorderid = 单号、#setprice 清空、#setbprice = 0、radio 回「发单价」', async () => {
      LAYUI.open.mockImplementation(() => 7)
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      expect(panel.querySelector<HTMLInputElement>('#setorderid')!.value).toBe(ROW.order_no)
      expect(panel.querySelector<HTMLInputElement>('#setprice')!.value).toBe('')
      expect(panel.querySelector<HTMLInputElement>('#setbprice')!.value).toBe('0')
      expect(
        panel.querySelector<HTMLInputElement>('input[name="pushPriceMode"][value="10"]')!.checked,
      ).toBe(true)
    // 脏值 → 再开一次必须复位（不复位会把上一单的价带进下一单）
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '999'
      panel.querySelector<HTMLInputElement>('#setbprice')!.value = '99'
      panel.querySelector<HTMLInputElement>('input[name="pushPriceMode"][value="20"]')!.checked = true
      root.querySelector('a.throwEdit')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      expect(panel.querySelector<HTMLInputElement>('#setprice')!.value).toBe('')
      expect(panel.querySelector<HTMLInputElement>('#setbprice')!.value).toBe('0')
      expect(
        panel.querySelector<HTMLInputElement>('input[name="pushPriceMode"][value="10"]')!.checked,
      ).toBe(true)
      cleanup()
    })

    it('保存：throwToHall(单号, 价) → 关弹层 + reload + 提示', async () => {
      LAYUI.open.mockImplementation(() => 7)
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      clickThrowSave(panel)
      expect(vi.mocked(throwToHall)).toHaveBeenCalledWith(ROW.order_no, '100.00')
      await vi.waitFor(() => {
        expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
        expect(LAYUI.table.reload).toHaveBeenCalledWith('datatable', expect.anything())
        expect(LAYUI.msg).toHaveBeenCalledWith('已发到内部接单大厅', { time: 1500 })
      })
      cleanup()
    })

    it('边界：`price = "0"` 是合法价（D-0015），照常提交（「空」判据不是真值判据）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '0'
      clickThrowSave(panel)
      expect(vi.mocked(throwToHall)).toHaveBeenCalledWith(ROW.order_no, '0')
      cleanup()
    })

    it('弹层保存按钮在 `.layui-layer-content` 里也照常工作（layui 会把节点搬出 root）', async () => {
      LAYUI.open.mockImplementation(() => 7)
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      // 模拟 layui 的搬运：节点离开 root、进 document.body 里的弹层容器
      const layerContent = document.createElement('div')
      layerContent.className = 'layui-layer-content'
      layerContent.appendChild(panel)
      document.body.appendChild(layerContent)
      expect(root.contains(panel)).toBe(false)
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      clickThrowSave(panel)
      expect(vi.mocked(throwToHall)).toHaveBeenCalledWith(ROW.order_no, '100.00')
      cleanup()
    })

    it('价为空的校验文案逐字取对标，且**一次请求都不发**', async () => {
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      clickThrowSave(panel) // #setprice 是空的
      expect(LAYUI.msg).toHaveBeenCalledWith('请填写金额和保证金，默认为0', { time: 1500 })
      expect(vi.mocked(throwToHall)).not.toHaveBeenCalled()
      cleanup()
    })

    it('价非法的文案按 pushPriceMode 分岔（10 → 金额 / 20 → 百分比）', async () => {
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = 'abc'
      clickThrowSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的金额，默认为0', { time: 1500 })
      LAYUI.msg.mockClear()
      panel.querySelector<HTMLInputElement>('input[name="pushPriceMode"][value="20"]')!.checked = true
      clickThrowSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的百分比数值(不需要带%)', { time: 1500 })
      expect(vi.mocked(throwToHall)).not.toHaveBeenCalled()
      cleanup()
    })

    it('价带 BOM（`\\ufeff100`）→ 按 Python 空白集判空并原样送校验，前端不先替后端裁掉', async () => {
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      // JS `.trim()` 会剥掉 U+FEFF，后端 Python `strip()` 不剥 ⇒ 前端若先 trim 就等于「比后端松」：
      // 我们放行、后端 `Decimal` 判 invalid。这里必须在前端就按 Python 口径拒掉（见 config/money.ts:87-90）。
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '\ufeff100'
      clickThrowSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的金额，默认为0', { time: 1500 })
      expect(vi.mocked(throwToHall)).not.toHaveBeenCalled()
      // 对照：普通空格两侧都裁，仍然合法；且提交的是**原始值**（后端按 Python 口径 strip），
      // 页面不再用自己的 `.trim()` 改用户输入。
      LAYUI.msg.mockClear()
      panel.querySelector<HTMLInputElement>('#setprice')!.value = ' 100.00'
      clickThrowSave(panel)
      expect(vi.mocked(throwToHall)).toHaveBeenCalledWith(ROW.order_no, ' 100.00')
      expect(LAYUI.msg).not.toHaveBeenCalledWith('请输入正确的金额，默认为0', { time: 1500 })
      cleanup()
    })

    it('保证金非法 → 提示「请输入正确的金额，填0代表不限制」，不发请求', async () => {
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      panel.querySelector<HTMLInputElement>('#setbprice')!.value = 'x'
      clickThrowSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的金额，填0代表不限制', { time: 1500 })
      expect(vi.mocked(throwToHall)).not.toHaveBeenCalled()
      cleanup()
    })

    it('选「按接单价的百分比设定」→ 提示尚未开放且不发请求（差异表 #14）', async () => {
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '50'
      panel.querySelector<HTMLInputElement>('input[name="pushPriceMode"][value="20"]')!.checked = true
      clickThrowSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('「按接单价的百分比设定」尚未开放', { time: 1500 })
      expect(vi.mocked(throwToHall)).not.toHaveBeenCalled()
      cleanup()
    })

    it('保证金是数值 0 的其它写法（`0.00`）→ 照常发到内部接单大厅，不落「尚未开放」分支', async () => {
      LAYUI.open.mockImplementation(() => 7)
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      panel.querySelector<HTMLInputElement>('#setbprice')!.value = '0.00'
      clickThrowSave(panel)
      // 判据是「保证金的值是不是 0」而不是「字符串等不等于 '0'」——字符串相等会把 '0.00' 误判成非零、
      // 弹「尚未开放」并且一次请求都不发（同一事实两套判据，E-0021 家族）。
      expect(vi.mocked(throwToHall)).toHaveBeenCalledWith(ROW.order_no, '100.00')
      expect(LAYUI.msg).not.toHaveBeenCalledWith('「用户最低缴纳保证金」尚未开放', { time: 1500 })
      cleanup()
    })

    it('保证金非 0 → 提示尚未开放且不发请求（差异表 #14）', async () => {
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      panel.querySelector<HTMLInputElement>('#setbprice')!.value = '50'
      clickThrowSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('「用户最低缴纳保证金」尚未开放', { time: 1500 })
      expect(vi.mocked(throwToHall)).not.toHaveBeenCalled()
      cleanup()
    })

    it('失败：显示后端 msg 原文（HTTP 403「你没有改接单价的权限」）且弹层不关', async () => {
      LAYUI.open.mockImplementation(() => 7)
      vi.mocked(throwToHall).mockRejectedValue(new HttpError(403, '你没有改接单价的权限'))
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      clickThrowSave(panel)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('你没有改接单价的权限', { time: 1500 })
      })
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })

    it('失败：业务码 1002（状态不对）同样显示后端 msg 原文，且弹层不关', async () => {
      LAYUI.open.mockImplementation(() => 7)
      vi.mocked(throwToHall).mockRejectedValue(
        new BizError(1002, '只有未分配的订单可以发到内部接单大厅，当前状态：WAITING_GRAB'),
      )
      const { root, cleanup } = await mountAndClickAction('throwEdit', ROW)
      const panel = root.querySelector<HTMLElement>('.setprice')!
      panel.querySelector<HTMLInputElement>('#setprice')!.value = '100.00'
      clickThrowSave(panel)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '只有未分配的订单可以发到内部接单大厅，当前状态：WAITING_GRAB',
          { time: 1500 },
        )
      })
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })
  })

  describe('行内「从内部接单大厅撤回」→ 对标确认语 + withdrawThrow', () => {
    it('确认语逐字；确认后 withdrawThrow(单号) 并 reload **当前页**', async () => {
      const { cleanup } = await mountAndClickAction('cancelThrowHome', WAITING_ROW)
      expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
      const [text, yes] = LAYUI.layer.confirm.mock.calls[0] as [string, (i: number) => void]
      expect(text).toBe('确定从内部接单大厅撤销该订单吗？撤销后，内部接单大厅将看不到该订单')
      expect(text).toBe(WITHDRAW_CONFIRM_TEXT)
      // 「当前页」的真出口是 `done` 交回的 `curr`（E-0037：本版本没有 `page(...)` 事件）；
      // 推到第 3 页才能分辨 `reload(currentPage)` 与 `reload()`（后者回第 1 页）。
      // 每页条数从 `getOptions` 读实时值（这里是默认的 20）。
      triggerDone({}, 3, 20)
      LAYUI.table.reload.mockClear()
      yes(3)
      expect(LAYUI.layer.close).toHaveBeenCalledWith(3)
      expect(vi.mocked(withdrawThrow)).toHaveBeenCalledWith(WAITING_ROW.order_no)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith('已从内部接单大厅撤回', { time: 1500 })
      })
      expect(LAYUI.table.reload).toHaveBeenCalledWith(
        'datatable',
        expect.objectContaining({ page: { curr: 3, limit: 20 } }),
      )
      cleanup()
    })

    it('确认之前**不发请求**（对标是 `layer.confirm` 的 yes 回调里才发）', async () => {
      const { cleanup } = await mountAndClickAction('cancelThrowHome', WAITING_ROW)
      expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
      expect(vi.mocked(withdrawThrow)).not.toHaveBeenCalled()
      cleanup()
    })

    it('失败显示后端 msg 原文（1002 的「只有待抢单可以撤回，当前状态：IN_PROGRESS」）', async () => {
      vi.mocked(withdrawThrow).mockRejectedValue(
        new BizError(1002, '只有待抢单可以撤回，当前状态：IN_PROGRESS'),
      )
      const { cleanup } = await mountAndClickAction('cancelThrowHome', WAITING_ROW)
      const yes = LAYUI.layer.confirm.mock.calls[0]![1] as (i: number) => void
      yes(3)
      await vi.waitFor(() => {
        expect(LAYUI.msg).toHaveBeenCalledWith(
          '只有待抢单可以撤回，当前状态：IN_PROGRESS',
          { time: 1500 },
        )
      })
      cleanup()
    })
  })

  // 工具栏那两颗「发内部 / 撤内部」已随 D-0093 从证据件与 `onToolbarClick` 里一起删除
  // （行内那颗的覆盖在上面两个 describe）；工具栏的 0 笔提示（`batchOrderNosOrNull()`）
  // 由「记退款」的用例覆盖。
})

/**
 * 「记退款」接后端（`POST /api/business/orders/<单号>/refunds`）。
 *
 * 口径：
 *  - 两个入口：工具栏「记退款」与「退款」列上的金额链接；
 *  - 弹窗四个字段按裁定删除（见模块头「已知差异 15」）；
 *  - 幂等键：**每次打开弹窗生成一个、弹窗内复用**（后端 `uniq_order_refund_request_id` 兜底）；
 *  - 失败**只弹后端 `msg` 原文**；其中 **`1010` 例外**：还要 `reload` 一次 + 关弹窗
 *    （这一笔服务端已落账，留着同一个键只会再拿 `1010`），其余码不关弹窗、不重取。
 *
 * 断言打在**真实出口**上（E-0009）：写请求断言的是 `request()` 收到的**真请求配置**
 * （`partialRefund` 走真身），不是页面自造的中间态。
 */
describe('「记退款」：两个入口 + 弹窗字段形状 + 提交 / 幂等 / 失败（T-P4-FE-3）', () => {
  const mockedRequest = vi.mocked(request)

  beforeEach(() => {
    LAYUI.open.mockReset()
    LAYUI.msg.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.layer.confirm.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.table.getOptions.mockReturnValue({ page: { limit: 20 } })
    LAYUI.table.checkStatus.mockReturnValue({ data: [] })
    LAYUI.form.render.mockClear()
    LAYUI.form.val.mockReturnValue({})
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    mockedRequest.mockReset()
    mockedRequest.mockResolvedValue({ amount: '50.00' } as never)
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
    // ⚠️ **必须在这里兜底卸载**：弹窗的「确定记录」是**文档级**捕获委托
    // （`document.addEventListener(..., true)`，因为 layui 会把节点搬出 `root`），
    // 而 `document.body.innerHTML = ''` **摘不掉文档级监听器**。用例中途断言失败时
    // （变异实验里必然发生）少调一次 `cleanup()`，上一个实例的监听器就会留在 document 上
    // ——下一次点击会被**两个**实例各处理一次，症状是「请求调用次数莫名多一」
    // （实测：变异 1 的第一轮跑出 6 条失败，其中 4 条是这种串扰，不是真判据）。
    teardown?.()
    teardown = null
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  /** 当前用例挂载出来的页面卸载函数（`afterEach` 兜底用）。 */
  let teardown: (() => void) | null = null

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    teardown = cleanup
    // ⚠️ **抹掉 mount 期的那一发 `request()`**（页 mount 会取一次 `/api/auth/me`
    //    `/api/auth/me` 决定行内入口渲不渲染）。本组有多处 `toHaveBeenCalledTimes(n)` 与
    //    `refundBodies()[0]` 这类「第一发就是退款」的判据 ⇒ 不抹掉它们会把 me 那一发算进去。
    mockedRequest.mockClear()
    return { root, cleanup }
  }

  function click(el: Element | null, what: string): void {
    expect(el, `${what} 不存在`).not.toBeNull()
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  function toolbarRefundBtn(root: HTMLElement): HTMLElement {
    return root.querySelector<HTMLElement>('button[data-tb="refundOrderBtn"]') as HTMLElement
  }

  /** 「退款」列的**真实 templet**（页面的 `refundRow`）——不手抄标记，避免与实现分叉。 */
  function renderRefundCell(row: typeof ROW): string {
    return (buildOrderCols(() => 'G', () => '')[14]!.templet as (d: typeof row) => string)(row)
  }

  /** 最近一次 `layer.open` 的入参。 */
  function lastOpenOpts(): Record<string, unknown> {
    const calls = LAYUI.open.mock.calls
    return calls[calls.length - 1]![0] as Record<string, unknown>
  }

  /** 每次写请求实际发出去的 body（经真 `partialRefund` → 被替身的 `request()`）。 */
  function refundBodies(): { refund_total: string; reason: string; request_id: string }[] {
    return mockedRequest.mock.calls.map(
      (c) =>
        (c[0] as { data: { refund_total: string; reason: string; request_id: string } }).data,
    )
  }

  /** 从工具栏入口挂载 + 打开弹窗；返回页内那个 `.setrefundOrderLog` 面板。 */
  async function openViaToolbar(): Promise<{
    root: HTMLElement
    cleanup: () => void
    panel: HTMLElement
  }> {
    LAYUI.open.mockImplementation(() => 9)
    const { root, cleanup } = await mountPage()
    renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], [ROW])[0]!.checked = true
    click(toolbarRefundBtn(root), '[data-tb=refundOrderBtn]')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const panel = root.querySelector<HTMLElement>('.setrefundOrderLog')
    expect(panel, '证据件里找不到 .setrefundOrderLog').not.toBeNull()
    return { root, cleanup, panel: panel! }
  }

  /**
   * 从**行内**「退款」列金额链接挂载 + 打开弹窗（spec §D11：**行内入口保持单笔**）。
   *
   * ⚠️ 工具栏那颗（`refundOrderBtn`）勾 1 笔也走**批量两步式**（spec §D11「勾 1 笔就是
   * 长度 1 的批量」）⇒ 下面 T2–T5 那些**单笔路径**的判据（`POST …/<单号>/refunds`、
   * `request_id` 复用、`1010` 单独处置、`reload` 保留当前页）只能由**行内**入口驱动。
   * ⛔ 判据本身一个字都没放宽。
   */
  async function openViaInline(): Promise<{
    root: HTMLElement
    cleanup: () => void
    panel: HTMLElement
  }> {
    LAYUI.open.mockImplementation(() => 9)
    const { root, cleanup } = await mountPage()
    const box = document.createElement('div')
    box.innerHTML = renderRefundCell({ ...ROW, refund_fee: '30.00' })
    root.appendChild(box)
    click(box.querySelector('a.refundFee'), 'a.refundFee')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const panel = root.querySelector<HTMLElement>('.setrefundOrderLog')
    expect(panel, '证据件里找不到 .setrefundOrderLog').not.toBeNull()
    return { root, cleanup, panel: panel! }
  }

  function fill(panel: HTMLElement, amount: string, reason: string): void {
    panel.querySelector<HTMLInputElement>('#refundFee')!.value = amount
    panel.querySelector<HTMLInputElement>('#refundRemark')!.value = reason
  }

  function clickSave(panel: HTMLElement): void {
    click(panel.querySelector('.saveReFundOrderBtn'), '.saveReFundOrderBtn')
  }

  describe('T1：两个入口都开同一个弹窗，且未提交前零请求', () => {
    it('入口①工具栏「记退款」：弹层参数逐项照对标 refundOrderLogs（L3903-3917）', async () => {
      const { cleanup } = await openViaToolbar()
      const opts = lastOpenOpts()
      expect(opts.title).toBe('记录订单退款')
      expect(opts.title).toBe(REFUND_DIALOG_TITLE)
      expect(opts.type).toBe(1)
      expect(opts.skin).toBe('layui-layer-lan')
      expect(opts.anim).toBe(5)
      expect(opts.area).toEqual(['720px', '600px'])
      expect(opts.shade).toEqual([0.3, '#333'])
      expect(opts.shadeClose).toBe(true)
      // 未提交 ⇒ 一次写请求都不发
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('入口②「退款」列的金额链接：开**同一个**弹层（`.setrefundOrderLog`），未提交前零请求', async () => {
      LAYUI.open.mockImplementation(() => 9)
      const { root, cleanup } = await mountPage()
      const box = document.createElement('div')
      box.innerHTML = renderRefundCell({ ...ROW, refund_fee: '30.00' })
      root.appendChild(box)
      click(box.querySelector('a.refundFee'), 'a.refundFee')
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      expect(lastOpenOpts().title).toBe('记录订单退款')
      // content 就是页内那个节点本身（同发价弹层的处置），不是另拼的标记
      expect(lastOpenOpts().content).toBe(root.querySelector('.setrefundOrderLog'))
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('未勾选任何行 ⇒ 提示「请选中要操作的记录」，不开弹窗、不发请求', async () => {
      LAYUI.table.checkStatus.mockReturnValue({ data: [] })
      const { root, cleanup } = await mountPage()
      click(toolbarRefundBtn(root), '[data-tb=refundOrderBtn]')
      expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录', { time: 1500 })
      expect(LAYUI.open).not.toHaveBeenCalled()
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('批量（勾选两行）⇒ 开**同一个**弹层（工具栏改走批量两步式，spec §D11），未提交前零请求', async () => {
      // ⚠️ 这颗按钮勾 1 笔也是**批量两步式**：开的是同一个弹层、并多渲染一句「每笔各退」
      // 口径说明；请求仍要等用户点「确定记录」才发（零请求）。
      // 批量的 URL / request_ids 在 `order-orderlist-batch.spec.ts` 里钉。
      const { root, cleanup } = await mountPage()
      renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], [ROW, { ...ROW, order_no: '20260920213215233030' }])
        .forEach((box) => { box.checked = true })
      click(toolbarRefundBtn(root), '[data-tb=refundOrderBtn]')
      expect(LAYUI.open).toHaveBeenCalledTimes(1)
      const panel = root.querySelector<HTMLElement>('.setrefundOrderLog')
      expect(panel, '证据件里找不到 .setrefundOrderLog').not.toBeNull()
      expect(panel!.textContent).toContain('批量记退款时，每一笔各设置成这个累计额（可能调高也可能调低）。')
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })
  })

  describe('T6：弹窗字段的最终形状（四处删除 + 三处保留，结构判据）', () => {
    it('弹窗里**没有**退款方式 / 支付单号 / 退款日期 / 同时改订单状态（含证据件原文）', async () => {
      const { panel, cleanup } = await openViaToolbar()
      for (const name of ['refundModel', 'paytranno', 'refundDate', 'refundChangeToYTD']) {
        expect(panel.querySelector(`[name="${name}"]`), `${name} 不该在弹窗里`).toBeNull()
      }
      for (const label of ['退款方式', '支付单号', '退款日期', '同时改订单状态']) {
        expect(panel.textContent).not.toContain(label)
      }
      // 证据件本身也必须已经是最终形状（防后来者「顺手加回来」）
      for (const name of ['refundModel', 'paytranno', 'refundDate', 'refundChangeToYTD']) {
        expect(EVIDENCE).not.toContain(`name="${name}"`)
      }
      cleanup()
    })

    it('保留「退款金额」`#refundFee` / 「备注」`#refundRemark` / 「确定记录」`.saveReFundOrderBtn`', async () => {
      const { panel, cleanup } = await openViaToolbar()
      expect(panel.querySelector('#refundFee')).not.toBeNull()
      expect(panel.querySelector('#refundRemark')).not.toBeNull()
      expect(panel.querySelector('.saveReFundOrderBtn')).not.toBeNull()
      expect(panel.textContent).toContain('退款金额')
      expect(panel.textContent).toContain('备注')
      expect(panel.textContent).toContain('确定记录')
      cleanup()
    })

    it('视觉值与「data-notopen」以证据件为准：特别注意引文 + `layui-btn-normal layui-btn-lg` + margin-left:110px，且保存按钮不再是「尚未开放」', async () => {
      const { panel, cleanup } = await openViaToolbar()
      const quote = panel.querySelector('blockquote.layui-elem-quote')
      expect(quote).not.toBeNull()
      expect(quote!.getAttribute('style')).toBe('font-size:16px;color:#ff0000')
      // ⭐（D-0312）金额口径是「累计退款总额」，⛔ 不是旧「本次增量」。
      //    断言打在**渲染后的 DOM 文本**上（这个 `panel` 就是打开弹窗时经 layui 搬进
      //    `.layui-layer-content` 的证据件节点，不是只 grep 证据件字符串）——`grep` 证据件
      //    只能证明文件里没那几个字，证明不了「用户屏幕上没有」。
      const quoteText = quote!.textContent ?? ''
      // ① 旧「本次增量」口径那句**不许再出现**（口径已统一成累计，照着旧文案填会写错数）。
      expect(quoteText).not.toContain('本次退款金额')
      expect(quoteText).not.toContain('多次退款会累加')
      // ② 「累计」口径必须在，且写明「填多少就是累计退了多少、可以改小」。
      expect(quoteText).toContain('累计退款金额')
      expect(quoteText).toContain('填多少就是累计退了多少')
      expect(quoteText).toContain('可以改小')
      // ③ 后半句（不要去改接单价…最终导致错账）**照抄保留**——两种口径下都成立
      expect(quoteText).toContain(
        '不要去改接单价，否则会形成又改接单价又记退款，最终导致错账',
      )
      // ④ 整句逐字钉住（措辞被改坏时这条先红，而不是靠上面几条子串兜住）
      expect(quoteText).toBe(
        '特别注意：如果接进来的订单出现退款或要退部分红包，就只需要在这里记录累计退款金额就行（填多少就是累计退了多少，可以改小），不要去改接单价，否则会形成又改接单价又记退款，最终导致错账',
      )
      expect(EVIDENCE).toContain('记录累计退款金额')
      expect(EVIDENCE).not.toContain('本次退款金额')
      // 字段 label 是「累计退款金额」。
      expect(panel.textContent).toContain('累计退款金额')
      const save = panel.querySelector<HTMLElement>('.saveReFundOrderBtn')!
      expect(save.className).toBe('layui-btn layui-btn-normal layui-btn-lg saveReFundOrderBtn')
      expect(save.getAttribute('style')).toBe('margin-left:110px;margin-top:20px')
      // `data-notopen` 已摘掉（不摘的话 `onNotOpenClick` 会把点击吞成「尚未开放」——
      // 先例是发价弹层的 `saveSettpricebtn`）
      expect(save.hasAttribute('data-notopen')).toBe(false)
      expect(EVIDENCE).not.toContain('data-notopen="记录订单退款"')
      cleanup()
    })

    it('第 18 列「退款」templet 照 `orderRefundRow`（L2090-2096）：>0 红 / =0 灰 + `.refundFee` + `data-fee` + 两种 lay-tips', () => {
      const withFee = renderRefundCell({ ...ROW, refund_fee: '30.00' })
      expect(withFee).toContain('class="refundFee"')
      expect(withFee).toContain('href="javascript:void(0)"')
      expect(withFee).toContain('color:red')
      expect(withFee).toContain(`data-fee="30.00"`)
      expect(withFee).toContain('>30.00</a>')
      expect(withFee).toContain('lay-tips="退款金额：30.00元，点击可以快速编辑退款"')
      const zero = renderRefundCell({ ...ROW, refund_fee: '0.00' })
      expect(zero).toContain('color:#999')
      expect(zero).toContain('lay-tips="没有退款，点击可以快速记录退款"')
      // ⛔ 没有「`refund_fee` 缺字段按 0 渲染」这一档：列表载荷**恒回**该键（spec §5.3 / C6），
      //    类型已收紧成必填——列表契约不保证的输入不再造用例。
    })

    it('`_refund` 本轮改成**默认显示**（业务方裁定 3），字段/标题/宽度/对齐照对标不动', () => {
      // 判据是**不写 `hide`**（本页「显示」的写法，同「订单来源」；`hide:false` 与不写等价）。
      const col = buildOrderCols(() => 'G', () => '')[14]
      expect(col).toMatchObject({
        field: '_refund',
        title: '退款',
        width: 60,
        align: 'center',
      })
      expect('hide' in (col as object)).toBe(false)
    })
  })

  describe('T2：提交的 URL 与 body 逐项断言（经真 `partialRefund`）', () => {
    it('POST /api/business/orders/<单号>/refunds，body `{refund_total, reason, request_id}` 字面值；`request_id` 非空且 ≤64', async () => {
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(mockedRequest).toHaveBeenCalledWith({
        method: 'post',
        url: `/api/business/orders/${ROW.order_no}/refunds`,
        data: { refund_total: '50.00', reason: '退货差价', request_id: expect.any(String) },
      })
      const id = refundBodies()[0]!.request_id
      expect(id.length).toBeGreaterThan(0)
      expect(id.length).toBeLessThanOrEqual(64)
      cleanup()
    })

    it('⭐ 行内单笔入口**预填当前累计**（`data-fee`），批量入口开空', async () => {
      // 行内：该行 `refund_fee = '30.00'` ⇒ 框里预填 30.00（「改小/改回去」的入口）。
      const inline = await openViaInline()
      const inlineFee = inline.panel.querySelector<HTMLInputElement>('#refundFee')!
      expect(inlineFee.value).toBe('30.00')
      inline.cleanup()

      // 批量：勾两行 ⇒ 没有「本单当前值」可填 ⇒ 开空。
      const { root, cleanup } = await mountPage()
      renderOrderRows(root, LAYUI.table.render.mock.lastCall?.[0], [ROW, { ...ROW, order_no: '20260920213215233030' }])
        .forEach((box) => { box.checked = true })
      click(toolbarRefundBtn(root), '[data-tb=refundOrderBtn]')
      const panel = root.querySelector<HTMLElement>('.setrefundOrderLog')!
      expect(panel.querySelector<HTMLInputElement>('#refundFee')!.value).toBe('')
      cleanup()
    })

    it('金额形态不对（空 / `abc`）⇒ 对标的原文提示且**一次请求都不发**', async () => {
      const { panel, cleanup } = await openViaInline()
      // 行内入口会预填当前累计（30.00）⇒ 先清空，才能真正考「空输入」。
      fill(panel, '', '')
      clickSave(panel)
      expect(LAYUI.msg).toHaveBeenCalledWith('请输入正确的金额', { time: 1500 })
      expect(LAYUI.msg).toHaveBeenCalledWith(REFUND_INVALID_AMOUNT_MESSAGE, { time: 1500 })
      fill(panel, 'abc', '退货差价')
      clickSave(panel)
      expect(mockedRequest).not.toHaveBeenCalled()
      cleanup()
    })

    it('边界：`refund_total = "0"` 形态合法 ⇒ 照发（`0` = 清空，由**后端**判）——前端不替后端加下界', async () => {
      const { panel, cleanup } = await openViaInline()
      fill(panel, '0', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(refundBodies()[0]!.refund_total).toBe('0')
      cleanup()
    })

    it('边界：备注留空**前端不拦**（必填由后端判）⇒ 照发空 `reason`，由后端 msg 说话', async () => {
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '')
      clickSave(panel)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      expect(refundBodies()[0]!.reason).toBe('')
      cleanup()
    })
  })

  describe('T3：幂等键（同一弹窗复用 / 关掉再开换新）', () => {
    it('同一弹窗内连续提交两次（第一次失败后重试）⇒ 两次 `request_id` **相同**', async () => {
      mockedRequest.mockRejectedValueOnce(
        new BizError(1007, '订单当前状态为「代练中」，只有「已完成」的订单可以部分退款'),
      )
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      // 失败 ⇒ 弹窗保持打开（下面另有断言）⇒ 用户再点一次「确定记录」
      clickSave(panel)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const [first, second] = refundBodies()
      expect(first!.request_id).toBe(second!.request_id)
      expect(first!.request_id.length).toBeGreaterThan(0)
      cleanup()
    })

    it('关掉弹窗再打开 ⇒ **不同的** `request_id`（新的一次操作）', async () => {
      const { root, panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(1))
      const first = refundBodies()[0]!.request_id
      // 用户在 layui 里关掉弹层：layui 会调 `end`（替身不会自己调，所以手动驱动真出口）
      ;(lastOpenOpts().end as () => void)()
      LAYUI.open.mockImplementation(() => 9)
      // 再从**行内**入口开一次（同一条单笔路径；工具栏那颗走批量）。
      const box2 = document.createElement('div')
      box2.innerHTML = renderRefundCell({ ...ROW, refund_fee: '30.00' })
      root.appendChild(box2)
      click(box2.querySelector('a.refundFee'), 'a.refundFee')
      const panel2 = root.querySelector<HTMLElement>('.setrefundOrderLog')!
      fill(panel2, '50.00', '退货差价')
      clickSave(panel2)
      await vi.waitFor(() => expect(mockedRequest).toHaveBeenCalledTimes(2))
      const second = refundBodies()[1]!.request_id
      expect(second).not.toBe(first)
      cleanup()
    })

    it('`newRefundRequestId()` 的形状：非空、≤64（后端列宽）且两次调用不同', () => {
      const a = newRefundRequestId()
      const b = newRefundRequestId()
      expect(a.length).toBeGreaterThan(0)
      expect(a.length).toBeLessThanOrEqual(64)
      expect(b).not.toBe(a)
    })

    it('回落分支（非安全上下文没有 `crypto.randomUUID`）：仍非空、唯一、长度 ≤64', () => {
      // `Crypto.randomUUID` 是**受安全上下文限制**的方法：局域网明文 `http://192.168.x.x`
      // 这类非安全上下文里它不存在 ⇒ 走时间戳 + 随机数的回落分支（`refund-…`）。
      // 这条分支正是生产里最可能被走到的（明文部署）。
      vi.stubGlobal('crypto', {})
      const a = newRefundRequestId()
      const b = newRefundRequestId()
      expect(a).toMatch(/^refund-/)
      expect(a.length).toBeGreaterThan(0)
      expect(a.length).toBeLessThanOrEqual(64)
      expect(b).not.toBe(a)
    })
  })

  describe('T4：**除 1010 外**的失败码逐类 ⇒ 弹**后端 msg 原文**、不重取、不关弹窗、不改状态', () => {
    // ⚠️ `1010` **不在**这张表里：它的处置与其余码**不同**（要 reload + 关弹窗），
    // 由下面 `T4b` 单独钉住——两个 describe 的标题也把这条差异写明了，别合并回去。
    const BIZ_FAILURES: readonly (readonly [number, string])[] = [
      [1007, '订单当前状态为「代练中」，只有「已完成」的订单可以部分退款'],
      [1008, '退款金额必须大于 0'],
      [1000, '订单不存在或不可见'],
    ]

    for (const [code, msg] of BIZ_FAILURES) {
      it(`业务码 ${code} ⇒ 弹「${msg}」`, async () => {
        mockedRequest.mockRejectedValue(new BizError(code, msg))
        const { panel, cleanup } = await openViaInline()
        fill(panel, '50.00', '退货差价')
        clickSave(panel)
        await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(msg, { time: 1500 }))
        // ⛔ 不许假装成功 / 不许自编文案
        expect(LAYUI.msg).not.toHaveBeenCalledWith(REFUND_SUCCESS_MESSAGE, { time: 1500 })
        // 失败**不重取**（本地状态也不动：本页没有任何乐观改写）
        expect(LAYUI.table.reload).not.toHaveBeenCalled()
        // 失败**不关弹窗**（用户留在弹窗里改金额 / 备注再试）
        expect(LAYUI.layer.close).not.toHaveBeenCalled()
        cleanup()
      })
    }

    it('HTTP 403（无 order.online_refund 权限）⇒ 弹后端的「你没有订单退款权限」', async () => {
      mockedRequest.mockRejectedValue(new HttpError(403, '你没有订单退款权限'))
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() =>
        expect(LAYUI.msg).toHaveBeenCalledWith('你没有订单退款权限', { time: 1500 }),
      )
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })

    it('HTTP 401（会话失效）⇒ 弹 `errors.ts` 的「未认证」，同样不重取、不关弹窗', async () => {
      mockedRequest.mockRejectedValue(new AuthExpiredError('未认证'))
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('未认证', { time: 1500 }))
      expect(LAYUI.table.reload).not.toHaveBeenCalled()
      expect(LAYUI.layer.close).not.toHaveBeenCalled()
      cleanup()
    })
  })

  describe('T4b：`1010`（幂等键重复）单独处置 ⇒ 弹 msg + 刷新一次 + 关弹窗', () => {
    const MSG_1010 = '该退款请求已处理过（request_id 重复）'

    it('业务码 1010 ⇒ 弹后端 msg、`reload` 一次、`layer.close` 一次——这一笔服务端已落账，别让用户守着同一个键', async () => {
      mockedRequest.mockRejectedValue(new BizError(1010, MSG_1010))
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith(MSG_1010, { time: 1500 }))
      // ① 仍弹**后端原文**（不许自编文案、不许假装成功）
      expect(LAYUI.msg).not.toHaveBeenCalledWith(REFUND_SUCCESS_MESSAGE, { time: 1500 })
      // ② 刷新一次：用户回到列表才能核到这一列 / 退款记录
      expect(LAYUI.table.reload).toHaveBeenCalledTimes(1)
      expect(LAYUI.table.reload.mock.calls[0]![0]).toBe('datatable')
      // ③ 关弹窗**恰好一次**：留着同一个键重试只会再拿 1010（弹层实例号来自
      //    `openViaToolbar` 的替身返回值 9）。`toHaveBeenCalledWith` 只证明「关过 9」，
      //    拦不住「关两层 / 关两次」这类回归 ⇒ 必须再加一条次数断言。
      expect(LAYUI.layer.close).toHaveBeenCalledWith(9)
      expect(LAYUI.layer.close).toHaveBeenCalledTimes(1)
      cleanup()
    })

    it('提示在 `reload` **之后**（reload 里的「筛选尚未开放」警告不许把 1010 的提示顶掉）', async () => {
      mockedRequest.mockRejectedValue(new BizError(1010, MSG_1010))
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalled())
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
      const msgOrder = LAYUI.msg.mock.invocationCallOrder[LAYUI.msg.mock.calls.length - 1]!
      expect(msgOrder).toBeGreaterThan(reloadOrder)
      cleanup()
    })
  })

  describe('T5：成功 ⇒ 关弹窗 + 重取**当前页与筛选** + 成功提示', () => {
    // ⚠️ 用例名里的 `limit` 与体内断言必须一致：本用例把「条/页」改成 **50** 再提交,
    // 所以断言的是 `{curr:3, limit:50}`——名字若写 20 会让下一个人以为覆盖的是默认每页条数。
    it('翻到第 3 页、每页改成 50、填了日期筛选后提交 ⇒ `reload` 带 `{curr:3, limit:50}` 与那两个筛选', async () => {
      const { panel, cleanup } = await openViaInline()
      // 用户把「条/页」改成 50（laypage 就地写进 config.page.limit）
      LAYUI.table.getOptions.mockReturnValue({ page: { limit: 50 } })
      // 用户在筛选栏填了录单时间区间（首屏快照是 mount 时的 `{}`，所以这算「用户填的」）
      LAYUI.form.val.mockReturnValue({
        sDate: '2026-07-01 00:00:00',
        eDate: '2026-09-21 23:59:59',
      })
      // 真人翻到第 3 页：layui 取完数后调 `done(res, 3, N)`（E-0037：这个版本没有 page 事件）
      triggerDone({}, 3, 40)
      LAYUI.table.reload.mockClear()

      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalled())

      expect(LAYUI.layer.close).toHaveBeenCalledWith(9)
      const call = LAYUI.table.reload.mock.calls[LAYUI.table.reload.mock.calls.length - 1]!
      expect(call[0]).toBe('datatable')
      const opts = call[1] as { page: { curr: number; limit: number }; where: Record<string, unknown> }
      expect(opts.page).toEqual({ curr: 3, limit: 50 })
      expect(opts.where).toEqual({
        date_from: expect.stringContaining('2026-07-01T00:00:00'),
        date_to: expect.stringContaining('2026-09-21T23:59:59'),
      })
      expect(LAYUI.msg).toHaveBeenCalledWith(REFUND_SUCCESS_MESSAGE, { time: 1500 })
      cleanup()
    })

    it('成功提示在 `reload` **之后**（reload 里的「筛选尚未开放」警告不许把成功提示顶掉）', async () => {
      const { panel, cleanup } = await openViaInline()
      fill(panel, '50.00', '退货差价')
      clickSave(panel)
      await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalled())
      const reloadOrder = LAYUI.table.reload.mock.invocationCallOrder[0]!
      const msgOrder = LAYUI.msg.mock.invocationCallOrder[LAYUI.msg.mock.calls.length - 1]!
      expect(msgOrder).toBeGreaterThan(reloadOrder)
      cleanup()
    })
  })
})

/**
 * 行内「一键发单到交易平台」那颗**地球按钮**（`.pushJdpEdit`）。
 *
 * 口径：
 *  - 按钮在「操作」列，class 含 `pushJdpEdit`，图标 `layui-icon-website`，
 *    `lay-tips`「把这笔订单一键发到各大交易平台」；
 *  - 对标是无条件渲染；本仓口径（D-0154）是「点了会 403 的入口就不该画」。
 *  - **入口探针**：`GET /api/business/platform-integrations/push-availability/`
 *    回 `{available: true}` 才画；403 / 网络 / 超时 / 形状不合 ⇒ **不画** +
 *    控制台一句原因（fail-closed）。
 *  - **可重发**：按 `platform_push.state` 判（`failed` / 无推送行 ⇒ 可重发；
 *    `sent/pending/unknown/closed` ⇒ 不画），⛔ 不用 `is_active` 当判据。
 *
 * 为什么挂载真页而不是只测纯函数：门的**数据来源**与「mount 那一刻定下来」是这一节最容易接错的两处
 * （漏了 `setPushJdpAvailable`、或在 `table.render` 之后才取数）。所以这里走 mount：
 * `fetch` 桩按 URL 回证据件与探针，再从**最近一次 `table.render` 的列定义**
 * 里取 `_change` 列的 `templet`，调它拿**真实产物**。
 */
describe('地球按钮（.pushJdpEdit）：入口探针 + 推送状态两道门', () => {
  /** 一条够 `btnfun` 跑通的最小行（`status` 非 WAITING_GRAB ⇒ 走 `.throwEdit` 分支）。 */
  const ROW = {
    order_no: '20260920213215233029',
    status: 'UNASSIGNED',
    source_order_no: '',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-20T21:32:15+08:00',
    // 行载荷上的推送摘要（`platform_push`）；「可重发」判据读它。
    platform_push: null as PlatformPushSummary | null,
  }

  /** 探针那一条 `fetch` 的应答（壳内可变：每个用例自己设）。 */
  let gate:
    | { mode: 'ok'; available: unknown }
    | { mode: 'forbidden' }
    | { mode: 'network' }
    | { mode: 'http500' } = { mode: 'ok', available: true }

  /** 后端答 `{available: true}`（登录 + `order.list` 通过）。 */
  function stubGateAvailable(): void {
    gate = { mode: 'ok', available: true }
  }

  /** 后端答 `{available: false}`（形状合法但明确说不可用）⇒ 不画。 */
  function stubGateUnavailable(): void {
    gate = { mode: 'ok', available: false }
  }

  /** 403：没有 `order.list` ⇒ 不该看到入口（fail-closed）。 */
  function stubGateForbidden(): void {
    gate = { mode: 'forbidden' }
  }

  /** 网络抛错：拿不到 ⇒ 不画（fail-closed）。 */
  function stubGateNetwork(): void {
    gate = { mode: 'network' }
  }

  /** 非 200（如 500）但 `fetch` 本身成功返回：走 `!res.ok` 那一支 ⇒ 不画。 */
  function stubGateHttp500(): void {
    gate = { mode: 'http500' }
  }

  /** 挂载整页（证据件与探针都走全局 fetch；其余请求走 `request` 桩）。 */
  async function mountPage(): Promise<() => void> {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
        }
        if (String(url).includes(PUSH_AVAILABILITY_URL)) {
          // ⚠️ **快照成局部 `const` 再收窄**：`gate` 是外层可变 `let`（还有别的函数给它赋值），
          //    `vue-tsc --build` 在下面的分支里**不肯**沿用 `gate.mode === …` 的收窄。
          const current = gate
          if (current.mode === 'network') throw new Error('network down')
          if (current.mode === 'http500') {
            return { ok: false, status: 500, json: async () => ({}) } as unknown as Response
          }
          if (current.mode === 'forbidden') {
            return { ok: false, status: 403, json: async () => ({}) } as unknown as Response
          }
          return {
            ok: true,
            status: 200,
            json: async () => ({ code: 0, data: { available: current.available } }),
          } as unknown as Response
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    return mountOrderListPage(root)
  }

  /** 最近一次 `table.render` 的「操作」列 templet（真实产物；⛔ 不是另建一份列定义）。 */
  function opTempletFromLastRender(): (d: typeof ROW) => string {
    const calls = LAYUI.table.render.mock.calls
    const opts = calls[calls.length - 1]?.[0] as
      | { cols?: Array<Array<{ field?: string; templet?: unknown }>> }
      | undefined
    const col = opts?.cols?.[0]?.find((c) => c.field === '_change')
    expect(col?.templet, 'table.render 的 _change 列应当带 templet（btnfun）').toBeTypeOf('function')
    return col!.templet as (d: typeof ROW) => string
  }

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('纯判据：探针载荷必须是 `{available: true}`；形状不合 / 真值软值一律 false', () => {
    expect(isPushAvailabilityAvailable({ available: true })).toBe(true)
    expect(isPushAvailabilityAvailable({ available: false })).toBe(false)
    // fail-closed：这些载荷都不许被当成「可用」。
    expect(isPushAvailabilityAvailable({})).toBe(false)
    expect(isPushAvailabilityAvailable(undefined)).toBe(false)
    expect(isPushAvailabilityAvailable(null)).toBe(false)
    expect(isPushAvailabilityAvailable(true)).toBe(false)
    // 「真值」不收：`1` / `'Y'` 不是后端字段的取值（后端是 JSON boolean）。
    expect(isPushAvailabilityAvailable({ available: 1 })).toBe(false)
    expect(isPushAvailabilityAvailable({ available: 'Y' })).toBe(false)
  })

  it('探针答 available=true ⇒ 按钮在（class / 图标 / lay-tips / title 逐字）', async () => {
    stubGateAvailable()
    const cleanup = await mountPage()
    const html = opTempletFromLastRender()(ROW)
    expect(html).toContain('pushJdpEdit')
    expect(html).toContain('layui-icon-website')
    expect(html).toContain('lay-tips="把这笔订单一键发到各大交易平台"')
    // 悬停提示真的能悬：vendored layui 不渲染 `lay-tips` ⇒ 这颗**同时**拼 `title`
    // （只有这一颗；其余行内按钮不加，见 `OP_BTN` 的 title 参数注释）。
    expect(html).toContain('title="把这笔订单一键发到各大交易平台"')
    cleanup()
  })

  it('探针答 available=false ⇒ **不画**（fail-closed），控制台留一句原因', async () => {
    const info = vi.spyOn(console, 'info').mockImplementation(() => {})
    try {
      stubGateUnavailable()
      const cleanup = await mountPage()
      const html = opTempletFromLastRender()({
        ...ROW,
        platform_push: {
          platform: 10,
          serial_no: '19000000000000000201',
          state: 'sent',
          remote_status: 11,
          remote_cancel_status: null,
          is_active: true,
        },
      })
      expect(html).not.toContain('pushJdpEdit')
      // 本页不提供平台写动作 ⇒ 探针不过时也不许冒出别的按钮。
      expect(html).not.toContain('pushJdpAmount')
      expect(html).not.toContain('pushJdpRepush')
      expect(html).not.toContain('pushJdpRevoke')
      expect(html).not.toContain('pushJdpCancel')
      expect(info).toHaveBeenCalledWith(expect.stringContaining('unavailable'))
      cleanup()
    } finally {
      info.mockRestore()
    }
  })

  it('403（没有 order.list）⇒ **不画**（fail-closed）：⛔ 不把「读不到」当成「可用」', async () => {
    stubGateForbidden()
    const cleanup = await mountPage()
    expect(opTempletFromLastRender()(ROW)).not.toContain('pushJdpEdit')
    cleanup()
  })

  it('推送失败（state=failed，is_active 仍是 true）⇒ 可重发，按钮照画（B2）', async () => {
    stubGateAvailable()
    const cleanup = await mountPage()
    const html = opTempletFromLastRender()({
      ...ROW,
      platform_push: {
        platform: 10,
        serial_no: '',
        state: 'failed',
        remote_status: null,
        remote_cancel_status: null,
        // ⚠️ `is_active=true`：判据已不看它——这个组合正是「旧判据会把按钮藏掉」那个方向的回归。
        is_active: true,
      },
    })
    expect(html).toContain('pushJdpEdit')
    cleanup()
  })

  /**
   * 按钮**永远在**；已抛过就点进去被拒绝（弹窗内 `can_push/reason` 说明），
   * ⛔ 不许靠隐藏按钮表达不可用。所以这里逐状态断言**照画**
   * （唯一不画的原因是入口探针 fail-closed）。
   */
  it.each([
    ['已发出', 'sent', true],
    ['发送中', 'pending', true],
    ['状态未知', 'unknown', true],
    ['已关闭', 'closed', false],
    ['发送失败', 'failed', true],
  ])('%s（state=%s）⇒ 按钮**仍常驻**（P4：不再按行数据门隐藏）', async (_label, state, isActive) => {
    stubGateAvailable()
    const cleanup = await mountPage()
    const html = opTempletFromLastRender()({
      ...ROW,
      platform_push: {
        platform: 10,
        serial_no: '19000000000000000201',
        state: state as PlatformPushSummary['state'],
        remote_status: 12,
        remote_cancel_status: null,
        // `is_active` 两个方向都出现过（failed=true / closed=false）⇒ 现在**一律不看它**。
        is_active: isActive,
      },
    })
    expect(html).toContain('pushJdpEdit')
    cleanup()
  })

  it('从未发过（platform_push=null）⇒ 按钮照画（首次发单入口）', async () => {
    stubGateAvailable()
    const cleanup = await mountPage()
    expect(opTempletFromLastRender()(ROW)).toContain('pushJdpEdit')
    cleanup()
  })

  it('网络抛错 ⇒ 拿不到 ⇒ 不画（fail-closed），但页面照常挂载', async () => {
    stubGateNetwork()
    const cleanup = await mountPage()
    expect(opTempletFromLastRender()(ROW)).not.toContain('pushJdpEdit')
    cleanup()
  })

  it('非 200（500，`!res.ok` 那一支）⇒ 不画（fail-closed）', async () => {
    stubGateHttp500()
    const cleanup = await mountPage()
    expect(opTempletFromLastRender()(ROW)).not.toContain('pushJdpEdit')
    cleanup()
  })

  it('门超时 3 秒 ⇒ 按拿不到处理（不画），⛔ 不把整页挂住', async () => {
    vi.useFakeTimers()
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn(
          (_url: unknown, init?: { signal?: AbortSignal }) =>
            new Promise<Response>((_resolve, reject) => {
              init?.signal?.addEventListener('abort', () => reject(new Error('AbortError')))
            }),
        ),
      )
      const pending = fetchPushAvailability()
      await vi.advanceTimersByTimeAsync(PUSH_JDP_GATE_TIMEOUT_MS)
      await expect(pending).resolves.toEqual({ state: 'unavailable' })
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  it('点它 ⇒ `layer.open` 装我们自己的岛页，标题 / 尺寸 / skin / anim 逐值照对标', async () => {
    stubGateAvailable()
    const cleanup = await mountPage()
    const row = { ...ROW, source_order_no: 'SRC-20260920-001' }
    const html = opTempletFromLastRender()(row)
    // 用真实产物造一颗节点（templet 产出的就是 `<a …>` 字符串）。
    const holder = document.createElement('div')
    holder.innerHTML = html
    const btn = holder.querySelector<HTMLElement>('a.pushJdpEdit')
    expect(btn, '门开着时 templet 产物里应当有 a.pushJdpEdit').not.toBeNull()
    // 把节点挂进页面 root（点击委托在 root 上）——`table.render` 是替身，表体不会自己长出来。
    const root = document.body.querySelector('div') as HTMLElement
    root.appendChild(holder)

    LAYUI.layer.open.mockClear()
    btn!.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.layer.open.mock.calls[0]![0] as {
      title: string
      type: number
      skin: string
      anim: number
      maxmin: boolean
      shadeClose: boolean
      shade: [number, string]
      area: [string, string]
      content: string
    }
    // 标题 = **展示单号** + 固定文案（中间无空格；对标逐字）。
    expect(opts.title).toBe('SRC-20260920-001发单到交易平台：请确认发单内容')
    expect(opts.type).toBe(2)
    expect(opts.skin).toBe('layui-layer-lan')
    expect(opts.anim).toBe(8)
    expect(opts.maxmin).toBe(true)
    expect(opts.shadeClose).toBe(true)
    expect(opts.shade).toEqual([0.3, '#333'])
    expect(opts.area[0]).toBe('1280px')
    expect(opts.area[1]).toBe(pushJdpLayerHeight())
    // content 用**内部单号**（`data-id`），不是展示单号。
    expect(opts.content).toContain('layui-island/page.html?page=order-pushjdp')
    expect(opts.content).toContain(`order_no=${row.order_no}`)
    cleanup()
  })

  it('展示单号是自由文本 ⇒ 进 layer 标题前再转义一次（dataset 会把 templet 那层解码回来）', async () => {
    stubGateAvailable()
    const cleanup = await mountPage()
    const row = { ...ROW, source_order_no: '<img src=x onerror=alert(1)>' }
    const holder = document.createElement('div')
    holder.innerHTML = opTempletFromLastRender()(row)
    const root = document.body.querySelector('div') as HTMLElement
    root.appendChild(holder)
    LAYUI.layer.open.mockClear()
    holder
      .querySelector<HTMLElement>('a.pushJdpEdit')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    const opts = LAYUI.layer.open.mock.calls[0]![0] as { title: string }
    expect(opts.title).not.toContain('<img')
    expect(opts.title).toContain('&lt;img')
    cleanup()
  })

  it('屏高分支逐值照对标：≤1080→95% / 1081..1440→90% / 1441..1600→80% / >1600→70%', () => {
    expect(pushJdpLayerHeight(768)).toBe('95%')
    expect(pushJdpLayerHeight(1080)).toBe('95%')
    expect(pushJdpLayerHeight(1081)).toBe('90%')
    expect(pushJdpLayerHeight(1440)).toBe('90%')
    expect(pushJdpLayerHeight(1441)).toBe('80%')
    expect(pushJdpLayerHeight(1600)).toBe('80%')
    expect(pushJdpLayerHeight(1601)).toBe('70%')
  })
})

/**
 * ⭐ P3 / P6 / P7 的**挂载级**判据（真 templet 产物 + root 上的点击委托 + 真 `request` 出口）。
 *
 * 手法沿用本文件的既有约定：`table.render` 是替身（jsdom 里跑不了真 layui）⇒ 自己造一份
 * **layui 形状**的表体（主表 + `fixed:'right'` 那张固定列表），把**真实 templet** 的产物塞进去，
 * 再点它。这样断言的是「页面渲染出来的那颗按钮被点之后真的打了哪个端点」，⛔ 不是测试自己
 * 拼的标记。
 */
describe('P3/P6/G2：行内平台动作（撤回 / 协商撤销 / 三角展开）的落地调用', () => {
  const ORDER_NO = '20260921000000000001'
  const ROW_BASE = {
    order_no: ORDER_NO,
    source_order_no: '',
    status: 'UNASSIGNED',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '1.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
  }
  const cols = buildOrderCols(
    () => 'G',
    () => '',
  )
  const orderIdCell = (push: PlatformPushSummary | null): string =>
    (cols[1]!.templet as (r: Record<string, unknown>) => string)({
      ...ROW_BASE,
      platform_push: push,
    })
  const actionCell = (push: PlatformPushSummary | null): string =>
    (cols[22]!.templet as (r: Record<string, unknown>) => string)({
      ...ROW_BASE,
      platform_push: push,
    })
  const push = (overrides: Partial<PlatformPushSummary> = {}): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 11,
    remote_cancel_status: 10,
    is_active: true,
    ...overrides,
  })
  const pushRow = (overrides: Partial<DailiantongPushRow> = {}): DailiantongPushRow => ({
    id: 1,
    order: 1,
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    state_display: '已发出',
    price: '12.50',
    ensure1: '0.00',
    ensure2: '0.00',
    time_limit_hours: 720,
    ex_order_no: ORDER_NO,
    remote_status: 12,
    remote_cancel_status: 10,
    accept_uid: '',
    last_synced_at: null,
    last_error: '',
    attempts: 1,
    created_at: '2026-10-27T12:00:00+08:00',
    updated_at: '2026-10-27T12:00:00+08:00',
    ...overrides,
  })

  type Config = { method?: string; url?: string; data?: Record<string, unknown> }
  /** 每个用例改这三个出口。 */
  let routeRows: () => Promise<DailiantongPushRow[]> | DailiantongPushRow[]
  let routeWithdraw: (config: Config) => Promise<DailiantongPushRow> | DailiantongPushRow

  beforeEach(() => {
    setOrderListPermissions(ORDER_NODES_FOR_TESTS)
    setPushJdpAvailable(true)
    LAYUI.layer.open.mockReset()
    LAYUI.layer.open.mockImplementation(() => 7)
    LAYUI.layer.close.mockClear()
    LAYUI.layer.confirm.mockReset()
    LAYUI.layer.alert.mockReset()
    LAYUI.layer.prompt.mockReset()
    LAYUI.msg.mockClear()
    LAYUI.table.reload.mockClear()
    routeRows = () => [pushRow()]
    routeWithdraw = () => pushRow({ state: 'closed', remote_status: 11 })
    // ⚠️ 本文件没有全局 `clearAllMocks`：不清的话**上一个用例**的 `request` 调用会串到本组
    // 的「恰好 N 发」断言里（实测：单测那几条会看到 4–8 发）。
    vi.mocked(request).mockReset()
    vi.mocked(request).mockImplementation((async (config: Config) => {
      const url = String(config?.url ?? '')
      if (url === '/api/auth/me') return { user: ORDER_NODES_FOR_TESTS }
      if (url === orderPlatformPushUrl(ORDER_NO)) return routeRows()
      if (url === `${orderPlatformPushUrl(ORDER_NO)}withdraw/`) return routeWithdraw(config)
      return {}
    }) as never)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
        }
        if (String(url).includes(PUSH_AVAILABILITY_URL)) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ code: 0, data: { available: true } }),
          } as unknown as Response
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  /**
   * 挂载整页，再造一份 layui 形状的表体并把**真实 templet 产物**塞进去。
   *
   * `platform_push=null` 时订单号格没有三角（P6 的无推送态），其余时刻它就在。
   */
  async function mountWithFakeTable(
    platformPush: PlatformPushSummary | null,
  ): Promise<{
    root: HTMLElement
    cleanup: () => void
    tr: HTMLTableRowElement
    tbody: HTMLElement
    fixedTbody: HTMLElement
  }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)

    const view = document.createElement('div')
    view.className = 'layui-table-view'
    const box = document.createElement('div')
    box.className = 'layui-table-box'
    const body = document.createElement('div')
    body.className = 'layui-table-body layui-table-main'
    const tbody = document.createElement('tbody')
    const tr = document.createElement('tr')
    tr.dataset.index = '0'
    /** 造一个 layui 形状的格：`<td data-field><div class="layui-table-cell laytable-cell-1-0-N">` */
    const makeTd = (field: string, html: string, index: number): HTMLTableCellElement => {
      const td = document.createElement('td')
      td.dataset.field = field
      const cell = document.createElement('div')
      cell.className = `layui-table-cell laytable-cell-1-0-${index}`
      cell.innerHTML = html
      td.appendChild(cell)
      return td
    }
    // 6 格：勾选 / 订单号 / 角色名 / 状态 / 发单价 / 操作
    // （B3 的子行要按**父行骨架**克隆；角色名那一格在平台侧没有数据 ⇒ 子行里应显示「—」）
    tr.append(
      makeTd('_orderId0', '', 0),
      makeTd('_orderid', orderIdCell(platformPush), 1),
      makeTd('_role', '', 3),
      makeTd('_sta', '', 6),
      makeTd('_throw', '', 12),
      makeTd('_change', actionCell(platformPush), 22),
    )
    tbody.appendChild(tr)
    const table = document.createElement('table')
    table.appendChild(tbody)
    body.appendChild(table)

    const fixed = document.createElement('div')
    fixed.className = 'layui-table-fixed layui-table-fixed-r'
    const fixedBody = document.createElement('div')
    fixedBody.className = 'layui-table-body'
    const fixedTbody = document.createElement('tbody')
    const fixedTr = document.createElement('tr')
    fixedTr.dataset.index = '0'
    const fixedTd = document.createElement('td')
    fixedTd.dataset.field = '_change'
    const fixedCell = document.createElement('div')
    fixedCell.className = 'layui-table-cell laytable-cell-2-0-22'
    fixedCell.innerHTML = actionCell(platformPush)
    fixedTd.appendChild(fixedCell)
    fixedTr.appendChild(fixedTd)
    fixedTbody.appendChild(fixedTr)
    const fixedTable = document.createElement('table')
    fixedTable.appendChild(fixedTbody)
    fixedBody.appendChild(fixedTable)
    fixed.appendChild(fixedBody)

    box.appendChild(body)
    box.appendChild(fixed)
    view.appendChild(box)
    root.appendChild(view)
    return { root, cleanup, tr, tbody, fixedTbody }
  }

  function platformGetCalls(): Config[] {
    return vi
      .mocked(request)
      .mock.calls.map(([config]) => config as Config)
      .filter((config) => config.url === orderPlatformPushUrl(ORDER_NO))
  }

  function withdrawCalls(): Config[] {
    return vi
      .mocked(request)
      .mock.calls.map(([config]) => config as Config)
      .filter((config) => config.url === `${orderPlatformPushUrl(ORDER_NO)}withdraw/`)
  }

  /** `islandMsg` 会带 `{time:1500}` 第二个参数 ⇒ 只取最后一条的**文案**。 */
  function lastMsgText(): string {
    const calls = LAYUI.msg.mock.calls
    return String(calls[calls.length - 1]?.[0] ?? '')
  }

  it('P6：有推送 ⇒ 订单号左侧有三角（选择器存在）；无推送 ⇒ 不画', async () => {
    const withPush = await mountWithFakeTable(push())
    expect(withPush.tr.querySelector(PLATFORM_TREE_SELECTOR)).not.toBeNull()
    withPush.cleanup()

    const without = await mountWithFakeTable(null)
    expect(without.tr.querySelector(PLATFORM_TREE_SELECTOR)).toBeNull()
    without.cleanup()
  })

  it('P6：点三角 ⇒ 只取一次该单推送行，并在父行下方插入子行（主表 + 右侧固定列两张表都插）', async () => {
    const { tr, tbody, fixedTbody, root, cleanup } = await mountWithFakeTable(push())
    const triangle = tr.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)!
    triangle.click()
    await vi.waitFor(() => {
      expect(tbody.querySelectorAll(`.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(1)
    })
    expect(platformGetCalls()).toHaveLength(1)
    expect(platformGetCalls()[0]!.method).toBe('get')
    // 子行紧跟在父行下方（对标 `splice(indexToInsertAfter + 1, …)`）。
    expect(tr.nextElementSibling?.classList.contains(PLATFORM_CHILD_ROW_CLASS)).toBe(true)
    // 右侧固定列那张表也插一行（不插会与主行错行）。
    expect(fixedTbody.querySelectorAll(`.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(1)
    // 展开态：图标换成三角向下 + data-open=Y。
    expect(triangle.classList.contains('layui-icon-triangle-d')).toBe(true)
    expect(triangle.dataset.open).toBe('Y')
    // 展开区内容（B3 起是真行：平台徽标 / 平台单号 / 交易状态标签 / 发单价；其余格「—」）。
    const panel = root.querySelector<HTMLElement>(`.${PLATFORM_CHILD_ROW_CLASS}`)!
    expect(panel.textContent).toContain('通')
    expect(panel.textContent).toContain('19000000000000000201')
    expect(panel.textContent).toContain('正在代练')
    expect(panel.textContent).toContain('12.50')
    expect(panel.textContent).toContain(PLATFORM_CHILD_EMPTY_VALUE)
    // B3：格子数与父行一致（列宽/表头/行高/边框都是继承来的，不是自己画的）
    expect(panel.children.length).toBe(tr.children.length)
    // 勾选格空、订单号格有 20px 缩进、操作格有「编辑」
    expect(panel.children[0]!.textContent).toBe('')
    expect(panel.children[1]!.innerHTML).toContain('margin-left:20px')
    expect(panel.children[1]!.querySelector('.layui-table-cell')).not.toBeNull()
    expect(panel.children[2]!.textContent).toBe(PLATFORM_CHILD_EMPTY_VALUE)
    expect(panel.children[5]!.innerHTML).toContain(PLATFORM_CHILD_EDIT_CLASS)
    // 右侧固定列表那条子行**只有操作一格**（它照着那张表的父行克隆）
    const fixedChild = fixedTbody.querySelector<HTMLElement>(`.${PLATFORM_CHILD_ROW_CLASS}`)!
    expect(fixedChild.children.length).toBe(1)
    expect(fixedChild.innerHTML).toContain(PLATFORM_CHILD_EDIT_CLASS)
    cleanup()
  })

  it('P6：再点三角 ⇒ 收起（两张表的子行都清掉），图标恢复向右', async () => {
    const { tr, tbody, fixedTbody, cleanup } = await mountWithFakeTable(push())
    const triangle = tr.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)!
    triangle.click()
    await vi.waitFor(() => {
      expect(tbody.querySelectorAll(`.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(1)
    })
    triangle.click()
    expect(tbody.querySelectorAll(`.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(0)
    expect(fixedTbody.querySelectorAll(`.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(0)
    expect(triangle.classList.contains('layui-icon-triangle-r')).toBe(true)
    expect(triangle.dataset.open).toBe('N')
    cleanup()
  })

  it('P6：连点三角 ⇒ 同一次加载只发一次取数（缓存 + 在途闸）', async () => {
    let resolveRows: (rows: DailiantongPushRow[]) => void = () => undefined
    routeRows = () => new Promise<DailiantongPushRow[]>((resolve) => {
      resolveRows = resolve
    })
    const { tr, tbody, cleanup } = await mountWithFakeTable(push())
    const triangle = tr.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)!
    triangle.click()
    triangle.click()
    triangle.click()
    expect(platformGetCalls()).toHaveLength(1)
    resolveRows([pushRow()])
    await vi.waitFor(() => {
      expect(tbody.querySelectorAll(`.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(1)
    })
    expect(platformGetCalls()).toHaveLength(1)
    cleanup()
  })

  it('P6：子行「编辑」⇒ 打开本系统订单详情层（能跳详情就跳，⛔ 不假装能编辑平台单）', async () => {
    const { tr, root, cleanup } = await mountWithFakeTable(push())
    tr.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)!.click()
    await vi.waitFor(() => {
      expect(root.querySelector(`.${PLATFORM_CHILD_EDIT_CLASS}`)).not.toBeNull()
    })
    const edit = root.querySelector<HTMLElement>(`.${PLATFORM_CHILD_EDIT_CLASS}`)!
    edit.click()
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.layer.open.mock.calls[0]![0] as { content: string; title: string }
    expect(opts.content).toContain('page=order-orderview')
    expect(opts.content).toContain(`order_no=${ORDER_NO}`)
    cleanup()
  })

  /* ── ⭐ P3：撤回箭头（撤回不在搬迁清单里，保留）───────────── */

  it('P3：确认语与两个按钮逐字照对标；确认前零请求', async () => {
    const { root, cleanup } = await mountWithFakeTable(push())
    root.querySelector<HTMLElement>(`.${PUSH_JDP_CANCEL_CLASS}`)!.click()
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    const [text, options] = LAYUI.layer.confirm.mock.calls[0] as [
      string,
      { btn: string[] },
      (index: number) => void,
    ]
    expect(text).toBe(PUSH_JDP_CANCEL_CONFIRM_TEXT)
    expect(options.btn).toEqual(['确定撤回', '我再想想'])
    expect(platformGetCalls()).toHaveLength(0)
    expect(withdrawCalls()).toHaveLength(0)
    cleanup()
  })

  it('P3：确认后 ⇒ 取该单推送行 → 逐平台 POST withdraw（载荷带 platform），成功后刷新并报结果', async () => {
    const { root, cleanup } = await mountWithFakeTable(push())
    root.querySelector<HTMLElement>(`.${PUSH_JDP_CANCEL_CLASS}`)!.click()
    const confirmCall = LAYUI.layer.confirm.mock.calls[0] as unknown as [
      string,
      unknown,
      (index: number) => void,
    ]
    confirmCall[2](1)
    await vi.waitFor(() => {
      expect(withdrawCalls()).toHaveLength(1)
    })
    expect(platformGetCalls()).toHaveLength(1)
    expect(withdrawCalls()[0]!.method).toBe('post')
    expect(withdrawCalls()[0]!.data).toEqual({ platform: 10 })
    // 成功后刷新（保持当前页）并如实报结果。
    expect(LAYUI.table.reload).toHaveBeenCalled()
    expect(LAYUI.layer.alert).toHaveBeenCalledTimes(1)
    expect(String(LAYUI.layer.alert.mock.calls[0]![0])).toContain('已撤回并退款')
    cleanup()
  })

  it('P3：没有推送 ⇒ 提示「这张单没有抛到任何平台」且**零写请求**（⛔ 不假装成功）', async () => {
    routeRows = () => []
    const { root, cleanup } = await mountWithFakeTable(push())
    root.querySelector<HTMLElement>(`.${PUSH_JDP_CANCEL_CLASS}`)!.click()
    const confirmCall = LAYUI.layer.confirm.mock.calls[0] as unknown as [
      string,
      unknown,
      (index: number) => void,
    ]
    confirmCall[2](1)
    await vi.waitFor(() => {
      expect(lastMsgText()).toBe(PUSH_JDP_NO_PLATFORM_MESSAGE)
    })
    expect(withdrawCalls()).toHaveLength(0)
    expect(LAYUI.table.reload).not.toHaveBeenCalled()
    cleanup()
  })

  it('P3：多平台逐平台回结果（部分成功如实报，失败原样显示后端中文）', async () => {
    routeRows = () => [
      pushRow({ platform: 10 }),
      pushRow({ id: 2, platform: 30, serial_no: 'MAMA-1' }),
    ]
    routeWithdraw = (config) => {
      if (config.data?.platform === 30) {
        return Promise.reject(new BizError(1002, '代练妈妈那边拒绝了这次撤回（后端原文）'))
      }
      return pushRow({ state: 'closed' })
    }
    const { root, cleanup } = await mountWithFakeTable(push())
    root.querySelector<HTMLElement>(`.${PUSH_JDP_CANCEL_CLASS}`)!.click()
    const confirmCall = LAYUI.layer.confirm.mock.calls[0] as unknown as [
      string,
      unknown,
      (index: number) => void,
    ]
    confirmCall[2](1)
    await vi.waitFor(() => {
      expect(withdrawCalls()).toHaveLength(2)
    })
    const alertText = String(LAYUI.layer.alert.mock.calls[0]![0])
    // 两个平台都各有一行结果（部分成功不许只报成功那半边）。
    expect(alertText).toContain('代练通')
    expect(alertText).toContain('代练妈妈')
    expect(alertText).toContain('已撤回并退款')
    expect(alertText).toContain('代练妈妈那边拒绝了这次撤回（后端原文）')
    // 有一台成功 ⇒ 刷新（切换成功的行要消失）。
    expect(LAYUI.table.reload).toHaveBeenCalled()
    cleanup()
  })

  it('P3：连点箭头 ⇒ 只走一轮（在途闸，⛔ 不重复撤回）', async () => {
    let resolveRows: (rows: DailiantongPushRow[]) => void = () => undefined
    routeRows = () => new Promise<DailiantongPushRow[]>((resolve) => {
      resolveRows = resolve
    })
    const { root, cleanup } = await mountWithFakeTable(push())
    const arrow = root.querySelector<HTMLElement>(`.${PUSH_JDP_CANCEL_CLASS}`)!
    arrow.click()
    const first = LAYUI.layer.confirm.mock.calls[0] as unknown as [
      string,
      unknown,
      (index: number) => void,
    ]
    first[2](1)
    // 第一次还在取数时再点一次（并确认）⇒ 不应再发。
    arrow.click()
    if (LAYUI.layer.confirm.mock.calls.length > 1) {
      const second = LAYUI.layer.confirm.mock.calls[1] as unknown as [
        string,
        unknown,
        (index: number) => void,
      ]
      second[2](1)
    }
    expect(platformGetCalls()).toHaveLength(1)
    resolveRows([pushRow()])
    await vi.waitFor(() => {
      expect(withdrawCalls()).toHaveLength(1)
    })
    cleanup()
  })

  /* ── ⭐ 短信那颗仍不许回来（协商撤销也已搬走）──────────────── */

  it('G2：短信那颗**已摘掉**（业务方明确不要）：标记里没有 sendsmsorderbtn / 发送短信 / 手机图标', () => {
    const html = actionCell(push({ remote_status: 13 }))
    expect(html).not.toContain('sendsmsorderbtn')
    expect(html).not.toContain('发送短信')
    expect(html).not.toContain('layui-icon-cellphone')
  })

  /**
   * ⭐ 「发代练通」那颗地球按钮**要一直都有**，不管订单是什么状态。
   * 渲染门**只有**入口探针 `pushJdpAvailable`（= 登录 + `order.list`；fail-closed）——
   * ⛔ 不看订单状态、⛔ 不看 `platform_push`、⛔ 不看有没有抛过、⛔ 不看是不是终态。
   * 已抛过 / 已关闭 / 代练中的行都要画出来，点开之后由弹窗里的 `can_push/reason`
   * 与后端错误说明「为什么不能发」。
   */
  it('地球按钮常驻：从未抛过 / 已抛过 / 已关闭 / 代练中 / 终态 —— **每一种都画**', () => {
    const cases: Array<[string, PlatformPushSummary | null]> = [
      ['从未抛过', null],
      ['已抛过（未接手 11）', push({ remote_status: 11 })],
      ['已关闭（本地 closed）', push({ state: 'closed', remote_status: 11 })],
      ['代练中（12）', push({ remote_status: 12 })],
      ['已结算（17，终态）', push({ remote_status: 17 })],
      ['发送失败（failed）', push({ state: 'failed', remote_status: null })],
    ]
    for (const [label, summary] of cases) {
      expect(actionCell(summary), `${label} 应当仍有地球按钮`).toContain(PUSH_JDP_CLASS)
    }
  })

  it('地球按钮：探针不通过（无配置 / 无 order.list）⇒ 不画（fail-closed）', () => {
    setPushJdpAvailable(false)
    try {
      expect(actionCell(push({ remote_status: 12 }))).not.toContain(PUSH_JDP_CLASS)
      expect(actionCell(null)).not.toContain(PUSH_JDP_CLASS)
    } finally {
      // 本组其它用例都依赖「探针通过」（`beforeEach` 会再设一次，这里恢复以免影响同组的后续断言）
      setPushJdpAvailable(true)
    }
  })

})

/* ═══════════ G1：勾选合计 / 跨页累计 / 陪·标记徽标 / 行内编辑 / 悬停 / 高亮 / 双击展开 ═══════════ */

/**
 * 差距表 `.superpowers/reports/gap-pages-list.md` §2 的 ❌1/2/3/4/7/8/9/10。
 */
describe('G1：订单列表补齐对标未复刻的交互（勾选合计 / 跨页 / 徽标 / 行内编辑 / 悬停 / 高亮 / 双击）', () => {
  const ORDER_A = '20260920213215233029'
  const ORDER_B = '20260920213215233030'
  const ORDER_C = '20260920213215233031'

  const ROW_BASE = {
    order_no: ORDER_A,
    source_order_no: '',
    status: 'UNASSIGNED',
    order_type: 'BOOST',
    tag: '',
    game: 2,
    server: null,
    project_text: 'x',
    total_price: '0.00',
    hall_price: '0.00',
    customer_phone: '',
    is_urgent: false,
    created_at: '2026-09-21T00:00:00+08:00',
    platform_push: null as PlatformPushSummary | null,
  }

  const ROW_A = { ...ROW_BASE, order_no: ORDER_A, total_price: '100.00', hall_price: '60.00' }
  const ROW_B = { ...ROW_BASE, order_no: ORDER_B, total_price: '20.50', hall_price: '9.50' }
  const ROW_C = { ...ROW_BASE, order_no: ORDER_C, total_price: '7.25', hall_price: '3.75' }

  const cols = buildOrderCols(
    () => 'G',
    () => '',
  )
  /** 取**真实 templet** 的订单号格产物（下标 1；不手抄标记，避免与实现分叉）。 */
  const orderCell = (overrides: Record<string, unknown> = {}): string =>
    (cols[1]!.templet as (r: Record<string, unknown>) => string)({
      ...ROW_BASE,
      ...overrides,
    })
  /** 取**真实 templet** 的代练/陪玩内容格产物。 */
  const projectCell = (overrides: Record<string, unknown> = {}): string =>
    (cols[4]!.templet as (r: Record<string, unknown>) => string)({
      ...ROW_BASE,
      ...overrides,
    })

  const push = (): PlatformPushSummary => ({
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    remote_status: 12,
    remote_cancel_status: null,
    is_active: true,
  })
  const pushRow = (): DailiantongPushRow => ({
    id: 1,
    order: 1,
    platform: 10,
    serial_no: '19000000000000000201',
    state: 'sent',
    state_display: '已发出',
    price: '12.50',
    ensure1: '0.00',
    ensure2: '0.00',
    time_limit_hours: 720,
    ex_order_no: ORDER_A,
    remote_status: 12,
    remote_cancel_status: null,
    accept_uid: '',
    last_synced_at: null,
    last_error: '',
    attempts: 1,
    created_at: '2026-10-27T12:00:00+08:00',
    updated_at: '2026-10-27T12:00:00+08:00',
  })

  beforeEach(() => {
    LAYUI.tableHandlers.clear()
    LAYUI.formHandlers.clear()
    LAYUI.msg.mockClear()
    LAYUI.layer.close.mockClear()
    LAYUI.layer.prompt.mockReset()
    LAYUI.layer.open.mockClear()
    LAYUI.table.reload.mockClear()
    LAYUI.table.checkStatus.mockReturnValue({ data: [] })
    vi.mocked(request).mockReset()
    vi.mocked(request).mockImplementation((async (config: { url?: string }) => {
      const url = String(config?.url ?? '')
      if (url === '/api/auth/me') return { user: ORDER_NODES_FOR_TESTS }
      if (url === orderPlatformPushUrl(ORDER_A)) return [pushRow()]
      return {}
    }) as never)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: unknown) => {
        if (String(url).includes('order-orderlist.html')) {
          return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
        }
        throw new Error(`未预期的 fetch：${String(url)}`)
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  async function mountPage(): Promise<{ root: HTMLElement; cleanup: () => void }> {
    const root = document.createElement('div')
    document.body.appendChild(root)
    const { mountOrderListPage } = await import('../order-orderlist')
    const cleanup = await mountOrderListPage(root)
    return { root, cleanup }
  }

  /**
   * 造出**页脚那个 `.selectTotal` span 的真身**。
   *
   * 证据件里它在 `#toolbarDemo` 的 `<script type="text/html">` 模板中（文字，不是子元素）；
   * 真浏览器里 layui 把模板灌进 `.layui-table-tool-temp`。jsdom 里 `table.render` 是替身，
   * 所以按同样层级手工插一个——被测代码的选择器与真实运行期一致。
   */
  function addSelectionTotalSpan(root: HTMLElement): HTMLElement {
    const tool = document.createElement('div')
    tool.className = 'layui-table-tool-temp'
    tool.innerHTML = '<span class="selectTotal"></span>'
    root.appendChild(tool)
    return tool.querySelector('.selectTotal') as HTMLElement
  }

  /**
   * 造一份 layui 形状的表体（表头 + `.layui-table-box .layui-table-body`）。
   *
   * `withTree=true` 时订单号格里带**真的**三角标记（`PLATFORM_TREE_SELECTOR`），用于双击展开。
   */
  function mountTableDom(
    root: HTMLElement,
    rows: { order_no: string }[],
    opts: { withTree?: boolean } = {},
  ): { tbody: HTMLElement; trs: HTMLTableRowElement[]; headCheckbox: HTMLInputElement } {
    root.querySelector('.layui-table-view')?.remove()
    const view = document.createElement('div')
    view.className = 'layui-table-view'

    const header = document.createElement('div')
    header.className = 'layui-table-header'
    header.innerHTML =
      '<table><thead><tr><th data-field="_orderId0">' +
      '<input type="checkbox" name="layTableCheckbox"></th></tr></thead></table>'
    const headCheckbox = header.querySelector('input') as HTMLInputElement

    const box = document.createElement('div')
    box.className = 'layui-table-box'
    const body = document.createElement('div')
    body.className = 'layui-table-body layui-table-main'
    const table = document.createElement('table')
    const tbody = document.createElement('tbody')
    const trs: HTMLTableRowElement[] = []
    rows.forEach((row, index) => {
      const tr = document.createElement('tr')
      tr.dataset.index = String(index)
      const cbTd = document.createElement('td')
      cbTd.dataset.field = '_orderId0'
      cbTd.innerHTML = '<div class="layui-table-cell"><input type="checkbox" name="layTableCheckbox"></div>'
      const orderTd = document.createElement('td')
      orderTd.dataset.field = '_orderid'
      const tree = opts.withTree
        ? `<i class="tree_btn layui-icon layui-icon-triangle-r" data-order="${row.order_no}" data-open="N"></i>`
        : ''
      orderTd.innerHTML =
        `<div class="layui-table-cell">${tree}<div id="order_${row.order_no}" class="orderLine">${row.order_no}</div></div>`
      tr.append(cbTd, orderTd)
      tbody.appendChild(tr)
      trs.push(tr)
    })
    table.appendChild(tbody)
    body.appendChild(table)
    box.appendChild(body)
    view.append(header, box)
    // ⚠️ 真证据件里**整张表都在** `<form class="layui-form">` 内 ⇒ 夹具也必须这样包，
    // 否则「双击被表单容器误挡」这类缺陷在 jsdom 里永远测不出来（实现里那条 `.layui-form`
    // 排除就是这么漏过去的）。
    const form = document.createElement('form')
    form.className = 'layui-form'
    form.appendChild(view)
    root.appendChild(form)
    return { tbody, trs, headCheckbox }
  }

  function fireCheckbox(payload: {
    type: 'one' | 'all'
    checked: boolean
    data?: Record<string, unknown>
  }): void {
    const handler = LAYUI.tableHandlers.get('checkbox(datatable)')
    expect(handler, '本页没有登记 checkbox(datatable) 事件').toBeTypeOf('function')
    handler!(payload)
  }

  function fireRow(tr: unknown): void {
    const handler = LAYUI.tableHandlers.get('row(datatable)')
    expect(handler, '本页没有登记 row(datatable) 事件').toBeTypeOf('function')
    handler!({ tr })
  }

  /** 经真实 `done` 出口模拟一次「取完数」（首帧 / 翻页 / 改条数都走它）。 */
  function loadPage(
    rows: { order_no: string }[],
    curr: number,
    total: number,
  ): void {
    triggerDone({ code: 0, msg: '', count: total, data: rows }, curr, total)
  }

  function click(el: Element | null, what: string): void {
    expect(el, `${what} 不存在`).not.toBeNull()
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  function selectRow(root: HTMLElement, orderNo: string, checked: boolean): void {
    const checkbox = root.querySelector<HTMLElement>(`#order_${orderNo}`)
      ?.closest('tr')?.querySelector<HTMLInputElement>('input[type="checkbox"]')
    expect(checkbox).not.toBeNull()
    checkbox!.checked = checked
    fireCheckbox({ type: 'one', checked })
  }

  describe('本页勾选与合计', () => {
    it('勾选只计当前页，反复勾同一笔不双计', async () => {
      const { root, cleanup } = await mountPage()
      const span = addSelectionTotalSpan(root)
      mountTableDom(root, [ROW_A, ROW_B])
      loadPage([ROW_A, ROW_B], 1, 3)

      selectRow(root, ORDER_A, true)
      selectRow(root, ORDER_A, true)
      expect(span.textContent).toBe('(勾选统计接单价：100，发单价：60)')
      selectRow(root, ORDER_B, true)
      expect(span.textContent).toBe('(勾选统计接单价：120.5，发单价：69.5)')
      selectRow(root, ORDER_A, false)
      expect(span.textContent).toBe('(勾选统计接单价：20.5，发单价：9.5)')
      selectRow(root, ORDER_B, false)
      expect(span.textContent).toBe('')
      cleanup()
    })

    it('负数用分累加，金额全为零时清空合计', async () => {
      const { root, cleanup } = await mountPage()
      const span = addSelectionTotalSpan(root)
      const rows = [
        { ...ROW_A, total_price: '-5.00', hall_price: '0.10' },
        { ...ROW_B, total_price: '0.20', hall_price: '-0.10' },
      ]
      mountTableDom(root, rows)
      loadPage(rows, 1, 2)
      selectRow(root, ORDER_A, true)
      selectRow(root, ORDER_B, true)
      expect(span.textContent).toBe('(勾选统计接单价：-4.8，发单价：0)')
      const zero = { ...ROW_A, total_price: '0.00', hall_price: null }
      mountTableDom(root, [zero])
      loadPage([zero], 1, 1)
      selectRow(root, ORDER_A, true)
      expect(span.textContent).toBe('')
      cleanup()
    })

    it('翻页及回翻会清空表头、行勾选及合计', async () => {
      const { root, cleanup } = await mountPage()
      const span = addSelectionTotalSpan(root)
      const first = mountTableDom(root, [ROW_A, ROW_B])
      loadPage([ROW_A, ROW_B], 1, 3)
      selectRow(root, ORDER_A, true)
      selectRow(root, ORDER_B, true)
      first.headCheckbox.checked = true

      const options = LAYUI.table.render.mock.lastCall?.[0] as { before: () => void }
      options.before()
      expect(first.headCheckbox.checked).toBe(false)
      expect(first.trs.every((tr) => !tr.querySelector('input')?.checked)).toBe(true)
      expect(span.textContent).toBe('')

      mountTableDom(root, [ROW_C])
      loadPage([ROW_C], 2, 3)
      selectRow(root, ORDER_C, true)
      expect(span.textContent).toBe('(勾选统计接单价：7.25，发单价：3.75)')

      const returned = mountTableDom(root, [ROW_A, ROW_B])
      loadPage([ROW_A, ROW_B], 1, 3)
      expect(returned.headCheckbox.checked).toBe(false)
      expect(returned.trs.every((tr) => !tr.querySelector('input')?.checked)).toBe(true)
      expect(span.textContent).toBe('')
      cleanup()
    })
  })

  describe('❌3 「陪」陪玩徽标 / ❌4 「标记」徽标（对标 1792-1794 / 1805-1807）', () => {
    it('陪玩单画「陪」（layui-bg-orange + 对标 tips），位置在「急」之前', () => {
      const html = orderCell({ order_type: 'COMPANION', is_urgent: true })
      expect(html).toContain(
        '<span class="lableb layui-bg-orange" lay-tips="这是一笔陪玩订单">陪</span>',
      )
      expect(html.indexOf('>陪</span>')).toBeLessThan(html.indexOf('>急</span>'))
    })

    it('非陪玩单不画「陪」；陪玩判定只认 `order_type === "COMPANION"`', () => {
      expect(orderCell({ order_type: 'BOOST' })).not.toContain('>陪</span>')
      expect(orderCell({ order_type: '' })).not.toContain('>陪</span>')
      expect(orderCell({ order_type: undefined })).not.toContain('>陪</span>')
    })

    it('有标记画「标记」徽标（#1E9FFF + `changeTag` + 前两个字符），急标在号码后且标记在「通」之前', () => {
      const html = orderCell({
        is_urgent: true,
        tag: '重点客户',
        platform_push: push(),
      })
      expect(html).toContain('<a href="javascript:void(0);" class="lableb changeTag"')
      expect(html).toContain('style="background-color:#1E9FFF"')
      expect(html).toContain(`data-tag="重点客户"`)
      expect(html).toContain('lay-tips="标记：重点客户，点击可以编辑"')
      // 只显示前两个字符。
      expect(html).toContain('>重点</a>')
      expect(html.indexOf('class="orderdetails"')).toBeLessThan(html.indexOf('>急</span>'))
      expect(html.indexOf('changeTag')).toBeLessThan(html.indexOf('>通</span>'))
    })

    it('标记空串 / 缺键 ⇒ 不画（⛔ 不占位、不显示「-」）', () => {
      expect(orderCell({ tag: '' })).not.toContain('changeTag')
      expect(orderCell({ tag: undefined })).not.toContain('changeTag')
    })

    it('标记是自由文本 ⇒ `data-tag` / `lay-tips` / 正文三处都过 `escapeHtml`', () => {
      const html = orderCell({ tag: 'a"<b>' })
      // 前两个字符 = `a"` ⇒ 正文转义成 `a&quot;`
      expect(html).toContain('>a&quot;</a>')
      expect(html).toContain('data-tag="a&quot;&lt;b&gt;"')
      expect(html).toContain('lay-tips="标记：a&quot;&lt;b&gt;，点击可以编辑"')
      expect(html).not.toContain('<b>')
    })
  })

  describe('❌4 行内改标记（对标 4265-4288）', () => {
    it('点「标记」徽标 ⇒ prompt（标题「编辑订单标记」/ 上限 50 / 预填当前值）→ PATCH `{tag}`', async () => {
      const { root, cleanup } = await mountPage()
      const holder = document.createElement('div')
      holder.innerHTML = orderCell({ tag: '重点客户' })
      root.appendChild(holder)

      click(holder.querySelector('a.changeTag'), 'a.changeTag')
      expect(LAYUI.layer.prompt).toHaveBeenCalledTimes(1)
      const call = LAYUI.layer.prompt.mock.calls[0] as unknown as [
        { formType: number; value: string; title: string; maxlength: number },
        (value: string, index: number) => void,
      ]
      expect(call[0]).toMatchObject({
        formType: 0,
        value: '重点客户',
        title: '编辑订单标记',
        maxlength: 50,
      })

      call[1]('新标记', 7)
      await vi.waitFor(() => {
        expect(vi.mocked(request)).toHaveBeenCalledWith({
          method: 'patch',
          url: `/api/business/orders/${ORDER_A}/`,
          data: { tag: '新标记' },
        })
      })
      // 连点闸（判据打在**这一种请求**上，不数 auth/kefu 那些挂载期的取数）：
      // 一次确认只发一次 PATCH（重复回调会发第二次，见 `openEditPrompt` 的 then）。
      const patches = vi
        .mocked(request)
        .mock.calls.map(([c]) => c as { method?: string; data?: { tag?: string } })
        .filter((c) => c.method === 'patch' && c.data?.tag !== undefined)
      expect(patches).toHaveLength(1)
      await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalled())
      expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
      expect(LAYUI.msg).toHaveBeenCalledWith('订单标记已更新', { time: 1500 })
      cleanup()
    })
  })

  describe('❌7 代练/陪玩内容行内快速编辑（对标 1899-1901 / 4781-4820）', () => {
    it('内容格是 `<a class="modityProject">`（可点），带 data-project / tips / 全文 title；无推送不加色', () => {
      const html = projectCell({ project_text: '代练到 60 级' })
      expect(html).toContain('class="modityProject"')
      expect(html).toContain('href="javascript:void(0)"')
      expect(html).toContain('data-project="代练到 60 级"')
      expect(html).toContain('lay-tips="代练/陪玩项目：代练到 60 级，点击可快速编辑"')
      expect(html).toContain('title="代练到 60 级"')
      expect(html).toContain('>代练到 60 级</a>')
      expect(html).not.toContain('color:')
    })

    it('有推送行 ⇒ 仍转紫 `#D15FE8`（B1 的既有口径，改成 `<a>` 后不能丢）', () => {
      expect(projectCell({ project_text: 'x', platform_push: push() })).toContain('#D15FE8')
    })

    it('项目文本是自由文本 ⇒ 正文 / data-project / tips / title 都转义', () => {
      const html = projectCell({ project_text: '<b>&"' })
      expect(html).toContain('&lt;b&gt;&amp;&quot;')
      expect(html).not.toContain('<b>')
    })

    it('点内容 ⇒ prompt（标题「请输入新的订单代练项目」/ 上限 100 / 预填当前值）→ PATCH `{project_text}`', async () => {
      const { root, cleanup } = await mountPage()
      const holder = document.createElement('div')
      holder.innerHTML = projectCell({ project_text: '代练到 60 级' })
      root.appendChild(holder)

      click(holder.querySelector('a.modityProject'), 'a.modityProject')
      expect(LAYUI.layer.prompt).toHaveBeenCalledTimes(1)
      const call = LAYUI.layer.prompt.mock.calls[0] as unknown as [
        { formType: number; value: string; title: string; maxlength: number },
        (value: string, index: number) => void,
      ]
      expect(call[0]).toMatchObject({
        formType: 0,
        value: '代练到 60 级',
        title: '请输入新的订单代练项目',
        maxlength: 100,
      })

      call[1]('代练到 70 级', 8)
      await vi.waitFor(() => {
        expect(vi.mocked(request)).toHaveBeenCalledWith({
          method: 'patch',
          url: `/api/business/orders/${ORDER_A}/`,
          data: { project_text: '代练到 70 级' },
        })
      })
      await vi.waitFor(() => expect(LAYUI.table.reload).toHaveBeenCalled())
      expect(LAYUI.layer.close).toHaveBeenCalledWith(8)
      expect(LAYUI.msg).toHaveBeenCalledWith('代练/陪玩项目已更新', { time: 1500 })
      cleanup()
    })
  })

  describe('❌8 单元格悬停显示全文 `title`（对标 2657-2662）', () => {
    it('每次取数完成后，每个 `.layui-table-cell` 的 `title` = 该格可见文本', async () => {
      const { root, cleanup } = await mountPage()
      mountTableDom(root, [ROW_A, ROW_B])
      loadPage([ROW_A, ROW_B], 1, 2)

      const cells = Array.from(root.querySelectorAll<HTMLElement>('.layui-table-cell'))
      expect(cells.length).toBeGreaterThanOrEqual(4)
      for (const cell of cells) {
        expect(cell.getAttribute('title')).toBe((cell.textContent ?? '').trim())
      }
      // 具体到订单号那一格：title 是全文，不是空串
      const orderCellEl = root.querySelector<HTMLElement>('#order_' + ORDER_A)!
        .parentElement as HTMLElement
      expect(orderCellEl.getAttribute('title')).toBe(ORDER_A)
      cleanup()
    })
  })

  describe('❌9 行点击高亮 `.selectTr`（对标 2431-2436）', () => {
    it('点一行 ⇒ 只该行有 `.selectTr`；点另一行 ⇒ 上一行的高亮被摘掉', async () => {
      const { root, cleanup } = await mountPage()
      const { trs } = mountTableDom(root, [ROW_A, ROW_B])

      fireRow(trs[0])
      expect(trs[0]!.classList.contains('selectTr')).toBe(true)
      expect(trs[1]!.classList.contains('selectTr')).toBe(false)

      fireRow(trs[1])
      expect(trs[0]!.classList.contains('selectTr')).toBe(false)
      expect(trs[1]!.classList.contains('selectTr')).toBe(true)

      // layui 交回的 `obj.tr` 是它自己的 jQuery 形状（`{0: <tr>}`）⇒ 两条形状都要认。
      fireRow({ 0: trs[0] })
      expect(trs[0]!.classList.contains('selectTr')).toBe(true)
      expect(trs[1]!.classList.contains('selectTr')).toBe(false)
      cleanup()
    })
  })

  describe('❌10 双击行展开 / 收起平台子单（对标 2359-2361）', () => {
    it('双击有推送的行 ⇒ 展开子行；再双击 ⇒ 收起；连点只取一次数（不插两份）', async () => {
      let resolveRows: (rows: DailiantongPushRow[]) => void = () => undefined
      vi.mocked(request).mockImplementation((async (config: { url?: string }) => {
        const url = String(config?.url ?? '')
        if (url === '/api/auth/me') return { user: ORDER_NODES_FOR_TESTS }
        if (url === orderPlatformPushUrl(ORDER_A)) {
          return new Promise<DailiantongPushRow[]>((resolve) => {
            resolveRows = resolve
          })
        }
        return {}
      }) as never)

      const { root, cleanup } = await mountPage()
      const { tbody, trs } = mountTableDom(root, [ROW_A], { withTree: true })
      const td = trs[0]!.children[1] as HTMLElement

      td.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
      // 连点：第一次取数还在飞 ⇒ 第二次不许再发（否则子行插两份）。
      td.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
      const pushCalls = (): number =>
        vi
          .mocked(request)
          .mock.calls.filter(([c]) => (c as { url?: string }).url === orderPlatformPushUrl(ORDER_A))
          .length
      expect(pushCalls()).toBe(1)

      resolveRows([pushRow()])
      await vi.waitFor(() => {
        expect(tbody.querySelectorAll(`tr.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(1)
      })
      const triangle = trs[0]!.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)!
      expect(triangle.dataset.open).toBe('Y')
      expect(triangle.classList.contains('layui-icon-triangle-d')).toBe(true)

      // 再双击 ⇒ 收起；收起**不再取数**（复用同一份缓存 / 纯 DOM 收起）。
      td.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
      expect(tbody.querySelectorAll(`tr.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(0)
      expect(triangle.dataset.open).toBe('N')
      expect(pushCalls()).toBe(1)
      cleanup()
    })

    it('双击没有三角（无推送）的行 ⇒ 什么都不做、零请求', async () => {
      const { root, cleanup } = await mountPage()
      const { tbody, trs } = mountTableDom(root, [ROW_B], { withTree: false })
      trs[0]!.children[1]!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
      await Promise.resolve()
      expect(tbody.querySelectorAll(`tr.${PLATFORM_CHILD_ROW_CLASS}`)).toHaveLength(0)
      const pushCalls = vi
        .mocked(request)
        .mock.calls.filter(([c]) => (c as { url?: string }).url === orderPlatformPushUrl(ORDER_B))
      expect(pushCalls).toHaveLength(0)
      cleanup()
    })

    it('双击三角本身不重复触发（单击已经负责展开/收起）', async () => {
      const { root, cleanup } = await mountPage()
      const { trs } = mountTableDom(root, [ROW_A], { withTree: true })
      const triangle = trs[0]!.querySelector<HTMLElement>(PLATFORM_TREE_SELECTOR)!
      triangle.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
      await Promise.resolve()
      const pushCalls = vi
        .mocked(request)
        .mock.calls.filter(([c]) => (c as { url?: string }).url === orderPlatformPushUrl(ORDER_A))
      expect(pushCalls).toHaveLength(0)
      cleanup()
    })
  })

  describe('缺数据源的三条登记在页面文件头「已知会不同」里（⛔ 不硬造）', () => {
    it('「会」会员单 / 「新」未读 / 黑名单图标 / 职业统计弹层 都写清了缺什么', () => {
      const source = readFileSync(path.resolve(HERE, '../order-orderlist.ts'), 'utf8')
      expect(source).toContain('会员单徽标（「会」）')
      expect(source).toContain('未读徽标（「新」）')
      expect(source).toContain('黑名单图标')
      expect(source).toContain('职业统计弹层')
      // 缺的是什么，必须可核对（缺字段 / 缺端点，不是「没做」三个字）。
      expect(source).toContain('customer_id')
      expect(source).toContain('is_read')
      expect(source).toContain('back_list_id')
      expect(source).toContain('没有对应端点')
    })
  })
})
