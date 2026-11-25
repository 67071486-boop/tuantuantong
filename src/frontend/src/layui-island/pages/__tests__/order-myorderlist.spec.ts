// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({ request: vi.fn() }))

/**
 * layui 的**可控替身**（与 `order-orderlist.spec.ts` 同一手法）。`bootstrapIsland` 在 jsdom 里
 * 加载不了真的 layui，所以换成替身之后 `layer.msg` / `layer.confirm` / `table.reload` 的
 * **入参**才能被断言、layui 自己的事件（toolbar / tool / submit / checkbox）才能手动触发。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const confirm = vi.fn()
  const close = vi.fn()
  // ⭐ T5：「点单号 ⇒ 原地开层」的落地出口（`layer.open` 的入参在这里被断言）。
  const open = vi.fn()
  const reload = vi.fn()
  const render = vi.fn()
  // ⭐ 「加急单」勾选后照对标弹一句 `layer.tips`（文案从元素的 `lay-tips` 读）。
  const tips = vi.fn()
  // ⭐ 「更改状态」对**勾选的行**生效 ⇒ 走 layui 的 `table.checkStatus`。
  const checkStatus = vi.fn(() => ({ data: [] as unknown[] }))
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  /**
   * 挂载期两个关键动作**真实发生的次序**（`bootstrap` = 挂 layui 样式/脚本，`artifact` = 取证据件标记）。
   * 用途见下面「先挂样式、再塞标记」那条用例：它必须由**运行期的调用序**判定，而不是只看源码行号——
   * 本文件把 `bootstrapIsland` 换成了替身，只有记在替身里才是「样式真的先挂上了」。
   */
  const order: string[] = []
  /**
   * `form.val(filter)` 的替身：**读真实 DOM**（真 layui 的 `form.val` 就是把表单控件的当前值
   * 收成一个 `name → value` 的扁对象）⇒ 「往输入框里填值 → 点搜索」这条链在测试里是**真交互**，
   * 而不是测试自己塞一个返回值进去（那样「选择器写错了」这类静默缺陷照样绿，AGENTS §5-G）。
   * ⚠️ 未勾选的 checkbox **不回键**（真 layui 的行为，页面代码按这条写）。
   */
  const formVal = vi.fn((filter: string) => {
    const el = document.querySelector<HTMLFormElement>(`form[lay-filter="${filter}"]`)
    const out: Record<string, string> = {}
    if (el === null) return out
    const controls = el.querySelectorAll<HTMLInputElement | HTMLSelectElement>('[name]')
    for (const control of Array.from(controls)) {
      if (
        control instanceof HTMLInputElement &&
        (control.type === 'checkbox' || control.type === 'radio')
      ) {
        if (control.checked) out[control.name] = control.value === '' ? 'on' : control.value
        continue
      }
      out[control.name] = control.value
    }
    return out
  })
  return {
    msg,
    confirm,
    close,
    open,
    reload,
    render,
    tips,
    checkStatus,
    formVal,
    tableHandlers,
    formHandlers,
    order,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
      val: formVal,
    },
    table: {
      render,
      reload,
      checkStatus,
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: { open, msg, confirm, close, tips },
    element: { init: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    // ⚠️ 写成**真正被调用时才执行**的函数（不是 `(async () => LAYUI)()` 那种立即求值的 Promise）：
    // 后者在 mock 定义时就跑完了，`order` 里会记不到「调用发生在哪一刻」。
    bootstrapIsland: (() => {
      LAYUI.order.push('bootstrap')
      return Promise.resolve(LAYUI)
    }) as unknown as typeof actual.bootstrapIsland,
  }
})

import { request } from '@/api/http'
import { HttpError, BizError } from '@/api/errors'
import {
  applyFilters,
  buildCols,
  buildCsv,
  CHANGE_STATUS_DIALOG_TITLE,
  CHANGE_STATUS_EVENT,
  CHANGE_STATUS_REQUIRES_IN_PROGRESS,
  csvCell,
  csvFilename,
  CSV_COLUMNS,
  defaultDateRange,
  EMPTY_FILTERS,
  escapeHtml,
  EXPORT_EMPTY_TEXT,
  EXPORT_EVENT,
  PUSH_IN_PROGRESS_TEXT,
  pushResultHasReason,
  pushResultText,
  REPORT_ISSUE_SUCCESS_TEXT,
  SELECT_RECORDS_FIRST_TEXT,
  setMyOrdersPermissions,
  statusesOfFilterValue,
  STATUS_BADGE_COLORS,
  STATUS_BADGE_FALLBACK,
  SUBMIT_CONFIRM_TEXT,
  SUBMIT_SUCCESS_TEXT,
  SYNC_TOOLBAR_BUTTON_CLASS,
  SYNC_TOOLBAR_BUTTON_LABEL,
  SYNC_TOOLBAR_EVENT,
  TOOLBAR_NOT_OPEN_EVENTS,
} from '../order-myorderlist'
// `MyOrderItem` 归 `api/myOrders` 所有，页面模块只是 import 它、没有转发 ⇒ 从源头取
// （`pnpm build` 会连 vitest 工程一起类型检查，这条只有它能抓到）。
import type { MyOrderItem } from '@/api/myOrders'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/order-myorderlist.html'),
  'utf8',
)

/**
 * 证据件**去掉 HTML 注释**之后的标记。
 *
 * ⚠️ 判「某个控件 / 按钮还在不在」时⛔ **不能拿整份 `EVIDENCE`**：文件头那段注释按本仓惯例
 * **会写出被删控件的名字**（那是留痕，不是标记）⇒ 拿整份文件当判据会得到**假红**。
 * 更糟的是反过来：如果为了让它变绿而把注释里的名字删掉，就等于抹掉留痕（AGENTS §4）。
 */
const EVIDENCE_MARKUP = EVIDENCE.replace(/<!--[\s\S]*?-->/g, '')

/**
 * 工具栏模板的**原文内容**（`#toolbarDemo`）。真 layui 会把它渲染进 `.layui-table-tool`；
 * jsdom 里没有真 layui，所以测试自己按同一层次注入——这样「尚未开放」的文案是从
 * **证据件**取的，不是测试里另抄一份中文。
 */
const TOOLBAR_HTML = /<script type="text\/html" id="toolbarDemo">([\s\S]*?)<\/script>/.exec(
  EVIDENCE,
)?.[1]

/**
 * 「订单状态」筛选下拉整段（`select[name=orderSta]`）的**原文**。守卫测试只认这一段里的
 * 可见 `<option>`——不拿「整份证据件里没有『待上号』」当判据（别处出现同词不该影响这条）。
 */
const STA_SELECT = /<select name="orderSta"[\s\S]*?<\/select>/.exec(EVIDENCE)?.[0] ?? ''

const mockedRequest = vi.mocked(request)

/**
 * 页面拿到的 `ctx.onNavigate`。⭐ **T5 起它是「负向对照」**：本页唯一的「进订单详情」入口
 * （单号蓝链）已改成**原地开层**，所以这条出口**一次都不该被调用**——`openOrderLayer` 用例里
 * 断言 `navigate` 未被调用，正是「不再跳路由」这条要求的判据（否则「改了」只能靠读代码）。
 */
const navigate = vi.fn()

/** 一行真数据（字段与后端 `MyOrderSerializer` 同形）。 */
const ROW: MyOrderItem = {
  id: 7,
  order_no: '20260920213215233029',
  // ⚠️ 基准行来源号为**空串**（回落变体的夹具）；「来源号非空 ⇒ 展示来源号」的主变体
  // 在下面 D-0301 那组用例里给。
  source_order_no: '',
  status: 'IN_PROGRESS',
  game_name: '原神',
  project_text: '深渊满星 12-3',
  temp_amount: '0.00',
  bonus_amount: '0.00',
  review_status: null,
  is_urgent: false,
  created_at: '2026-09-20T21:32:15+08:00',
  // `/my-orders/` 的载荷里恒有这三个字段（`MyOrderSerializer.Meta.fields`）——本页只渲染
  // `username` 之外的内容，但类型是必填的，夹具按后端词表写值（不是占位空串）。
  username: '打手小李',
  // ⭐ 接手人 / 接手价（本页第 12 / 13 列渲染它们）。
  // 显示名与登录账号**刻意不同值**（否则「昵称优先」与「恒取 username」两种实现都能绿）。
  taker_name: '打手昵称甲',
  // 后端金额出口是**两位小数字符串**（覆盖价 66.50 ≠ 夹具 `temp_amount`（0.00）⇒ 拿绩效金额
  // 冒充接手价的实现会在这条上红）。
  taker_price: '66.50',
  emp_type: '打单工资',
  settle_status: '未结账',
  // ⭐ D-0365：载荷带申请时刻（手机端本页不渲染，类型必填按后端形状补）。
  settle_requested_at: null,
}

function injectToolbar(root: HTMLElement): void {
  const wrap = document.createElement('div')
  wrap.className = 'layui-table-tool'
  wrap.innerHTML = TOOLBAR_HTML ?? ''
  root.appendChild(wrap)
}

type ReqConfig = { method?: string; url?: string }
type ReqImpl = (config: ReqConfig) => Promise<unknown>

/**
 * 权限门：`/api/auth/me` 的载荷决定「订单截图」两颗按钮渲不渲染。
 * ⭐ 「更改状态」要节点 `order.submit_work`（第三个门）。
 * 这个夹具 = **三个节点都有**（客服 / 管理员 / 代练员），默认用例因此看到的是功能齐全的那一档。
 * ⚠️ 节点 code 在这里写**字面值**（⛔ 不 import 生产常量）：自指的夹具改名时两边一起变、照样绿
 * （E-0024）。
 */
const SCREENSHOT_NODES = ['order.view_screenshot', 'order.upload_screenshot']
const CHANGE_STATUS_NODE_CODE = 'order.submit_work'
const ME_FULL = {
  user: {
    is_tenant_admin: false,
    is_account_admin: false,
    menu_permissions: [...SCREENSHOT_NODES, CHANGE_STATUS_NODE_CODE],
  },
}

/** 默认后端：列表回 `rows`，提交回流转后的订单，身份回三个节点（见 `ME_FULL`）。 */
function defaultImpl(rows: MyOrderItem[]): ReqImpl {
  return async (config) => {
    if (config.url === '/api/auth/me') return ME_FULL
    // ⭐ 「立即同步」也是 POST ⇒ 这条必须排在下面那条泛化的 `method === 'post'` **之前**，
    //    否则同步那一跳会拿到「提交完单」的响应体（页面拿到 `undefined` 的 `applied` 却不报错）。
    if (config.url === '/api/business/member/jidanpu-sync-now/') return { applied: 2 }
    // ⭐ 手动推送同理：也是 POST ⇒ 必须排在泛化的那条之前，
    //    否则页面拿到的是「提交完单」的 `{order_no, status}`，`pushed` / `skipped` 都是 `undefined`。
    //    计数用固定值（后端口径：pushed + skipped + failed === requested）。
    if (config.url === '/api/business/member/jidanpu-push-now/') {
      return { requested: 2, pushed: 1, skipped: 1, failed: 0, items: [] }
    }
    if (config.method === 'post') return { order_no: ROW.order_no, status: 'PENDING_REVIEW' }
    return { items: rows, total: rows.length, page: 1, page_size: 200 }
  }
}

/** 挂整页（取数走**真的** `api/myOrders.ts` → 打桩的 `request`）。 */
async function mountPage(impl: ReqImpl = defaultImpl([ROW])): Promise<HTMLElement> {
  mockedRequest.mockImplementation(impl as never)
  const root = document.createElement('div')
  document.body.appendChild(root)
  const { mountMyOrderListPage } = await import('../order-myorderlist')
  await mountMyOrderListPage(root, { onNavigate: navigate })
  injectToolbar(root)
  return root
}

/** 走一次「点提交完成 → 确认」的完整路径，返回 `layer.confirm` 的入参。 */
function clickSubmit(): Record<string, unknown> {
  LAYUI.tableHandlers.get('tool(tableList)')?.({ event: 'submtEnd', data: ROW })
  expect(LAYUI.confirm).toHaveBeenCalledTimes(1)
  const call = LAYUI.confirm.mock.calls[0] as unknown as [string, (i: number) => void]
  call[1](0)
  return { text: call[0] } as Record<string, unknown>
}

beforeEach(() => {
  mockedRequest.mockReset()
  // 权限门默认「三个节点都有」——直接调 `buildCols()[14].templet` 的那些用例（不挂页）照旧看到两颗按钮；
  // 门本身的用例自己会覆盖它（见 `describe('订单截图权限门')` 与 `describe('更改状态的权限门')`）。
  setMyOrdersPermissions({
    menu_permissions: [...SCREENSHOT_NODES, CHANGE_STATUS_NODE_CODE],
  })
  navigate.mockClear()
  LAYUI.msg.mockClear()
  LAYUI.confirm.mockClear()
  LAYUI.reload.mockClear()
  LAYUI.render.mockClear()
  LAYUI.tips.mockClear()
  LAYUI.checkStatus.mockReset()
  LAYUI.checkStatus.mockReturnValue({ data: [] as unknown[] })
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  LAYUI.order.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('order-myorderlist.html')) {
        LAYUI.order.push('artifact')
        return { ok: true, status: 200, text: async () => EVIDENCE }
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  // `vi.stubEnv('BASE_URL', …)`（T5 基址守卫）的兜底还原。
  vi.unstubAllEnvs()
  // 导出那几条用例 `vi.spyOn(HTMLAnchorElement.prototype, 'click')` ⇒ 必须还原，
  // 否则后面别的用例里 `.click()` 会变成空操作（**静默**：点了没反应）。
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('挂载顺序：**先挂 layui 样式，再塞页面标记**', () => {
  /**
   * 从「内部抢单大厅」切到本页时，页面框架会**从简陋闪一下变正常**——
   * 根因就是挂载顺序写反（先塞标记再挂样式，浏览器先画出没穿 `layui.css` 的骨架）。
   * 同批的大厅页（`order-insiderecorderlist`）是先挂样式再塞标记，
   * 所以「有的页面闪、有的不闪」。
   *
   * 判据取**调用序**（`['bootstrap', 'artifact']`）：把顺序写反就会变成 `['artifact', 'bootstrap']`，
   * 这条会当场变红。全站同类页面另有静态兜底网
   * （`src/layui-island/__tests__/page-mount-order.spec.ts`），本条只盯本页的运行期事实。
   */
  it('bootstrapIsland 先于取证据件（写反了就会闪一下没样式的页面）', async () => {
    await mountPage()
    expect(LAYUI.order).toEqual(['bootstrap', 'artifact'])
  })
})

describe('列表取数：真的走 GET /api/business/my-orders/', () => {
  it('挂载即发 GET（方法 + URL 字面值），并把返回行交给 layui 渲染', async () => {
    await mountPage()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/my-orders/',
      params: { page: 1, page_size: 200 },
    })
    const opts = LAYUI.render.mock.calls[0]?.[0] as { data?: MyOrderItem[] }
    expect(opts.data).toEqual([ROW])
  })

  it('无节点 `my_orders.send_to_my_system`（403）→ 后端 msg **原样**显示，且表被清空（不是静默空表）', async () => {
    await mountPage(async () => {
      // ⚠️ 闸门是**节点**（不是 `Role.TAKER`）：后端对无节点者回
      // 权限层的 403，文案由后端给。本用例只钉「前端把 msg 原样弹出来」，不钉具体文案。
      throw new HttpError(403, '你没有查看我的订单的权限')
    })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('你没有查看我的订单的权限', { time: 6000 })
    })
    const opts = LAYUI.render.mock.calls[0]?.[0] as { data?: MyOrderItem[] }
    expect(opts.data).toEqual([])
  })
})

/**
 * 本页 `COLS` 的下标（`order-myorderlist.ts` 里第 0 项是勾选列）：
 * `1=orderIdView / 9=orderSta / 14=field:''(订单截图) / 15=recOrderTime`。
 * 这些断言写的是**下标**（`CELL_TEMPLETS` 就是按下标填的），不是「第 N 列」的口头说法。
 */
describe('单元格渲染：只填后端真回了的列', () => {
  const cols = buildCols()
  const cell = (i: number, d: MyOrderItem = ROW): string => cols[i]!.templet(d)

  it('订单号列（下标 1）是对标那种蓝链 + 容器 id（`orderIdView` 的 templet，L738-771）', () => {
    const html = cell(1)
    expect(html).toContain('class="orderdetails"')
    expect(html).toContain('href="javascript:void(0);"')
    expect(html).toContain('color:#0094ff')
    expect(html).toContain('data-order="20260920213215233029"')
    // 对标把 `res.orderId`（**订单主键**）装进容器/复制 span 的 id 与 `data-id`；
    // `data-orderIdView` / 复制文本 / 链接 `data-viewId` 装**展示单号**（`res.orderIdView`）。
    // 我们的 `/my-orders/` **没有订单主键**（载荷的 `id` 是绩效行主键）⇒ 这些 id 位一律填 `order_no`。
    expect(html).toContain(`id="order_${ROW.order_no}"`)
    expect(html).toContain(`data-id="${ROW.order_no}"`)
    expect(html).toContain('data-orderIdView="20260920213215233029"')
    expect(html).toContain(`id="orderid_span_${ROW.order_no}"`)
    expect(html).toContain('data-clipboard-text="20260920213215233029"')
    expect(html).toContain('>20260920213215233029</a>')
    // 偏差不是空话：`ROW.id`（= 7）是**绩效行主键**（`api/myOrders.ts:46-48` 明确写不要拿它当
    // 订单标识），所以它**不该**出现在任何订单 id 位上——这条断言在退回 `String(d.id)` 时会红。
    expect(html).not.toContain(`data-id="${ROW.id}"`)
    expect(html).not.toContain(`id="order_${ROW.id}"`)
    expect(html).not.toContain(`id="orderid_span_${ROW.id}"`)
  })

  it('⭐ D-0301（2026-10-24）订单号列：**来源号非空 ⇒ 展示位逐字显示来源号**，寻址键照旧内置号', () => {
    const row: MyOrderItem = { ...ROW, source_order_no: 'SRC-TEST-1' }
    const html = cell(1, row)
    // 展示位（对标语义 = 展示单号）：`data-orderIdView` / 复制文本 / 链接 `data-viewId` / 正文。
    expect(html).toContain('data-orderIdView="SRC-TEST-1"')
    expect(html).toContain('data-clipboard-text="SRC-TEST-1"')
    expect(html).toContain('data-viewId="SRC-TEST-1"')
    expect(html).toContain('>SRC-TEST-1</a>')
    // 寻址键照旧内置号（右键目标 / 行内点击 / 详情取数都靠它们）。
    expect(html).toContain(`id="order_${ROW.order_no}"`)
    expect(html).toContain(`data-id="${ROW.order_no}"`)
    expect(html).toContain(`data-order="${ROW.order_no}"`)
    // ⛔ 正文里不再出现内置号（来源号非空时）。
    expect(html).not.toContain(`>${ROW.order_no}</a>`)
  })

  it('系统内置单号（下标 2）/ 游戏名称（6）/ 内容（7）仍是纯文本', () => {
    expect(cell(2)).toBe(ROW.order_no)
    expect(cell(6)).toBe('原神')
    expect(cell(7)).toBe('深渊满星 12-3')
  })

  it('后端没回的列保持空白（不拿别的字段冒充）', () => {
    // 3 角色名 / 5 阵营平台 / 8 角色信息 / 10 时间 / 11 旺旺来源（隐藏）/ 16 机位 / 18 日志
    // ⚠️ **12 接手人 / 13 接手价已不在本列表里**（那两列已接真，见下面两条）。
    for (const i of [3, 5, 8, 10, 11, 16, 18]) {
      expect(cell(i)).toBe('')
    }
  })

  it('接手人（下标 12）渲染后端的 `taker_name`；`null` ⇒ 空串', () => {
    // ⚠️ 下标是 **12**（`COLS` 第 0 项是勾选列；11 是隐藏的「旺旺/来源」）。
    // 值写**字面值**（E-0024）：夹具的 `taker_name` = '打手昵称甲'（与 `username` 不同）。
    expect(cell(12)).toBe('打手昵称甲')
    // `null` = 这一行还没有归属人 ⇒ 空串（⛔ 不是 'null' / 'undefined' / 自造占位）。
    expect(cell(12, { ...ROW, taker_name: null })).toBe('')
  })

  it('接手价（下标 13）渲染后端的 `taker_price`（两位小数字符串）；`null` ⇒ 空串', () => {
    // ⚠️ 钉**字面值**：后端已经格式化好，前端⛔ 不再量化 / 补 ¥ / 换算。
    expect(cell(13)).toBe('66.50')
    // 0.00 是**合法金额**（D-0015）⇒ 必须原样渲染，⛔ 不能被真值判断吃掉变成空串。
    expect(cell(13, { ...ROW, taker_price: '0.00' })).toBe('0.00')
    expect(cell(13, { ...ROW, taker_price: null })).toBe('')
  })

  it('接手人 / 接手价两列的值都被转义（`templet` 的返回值被 layui 当 HTML 插入）', () => {
    // 昵称是用户输入（`User.first_name`）⇒ 不转义就是存储型 XSS；接手价来自后端金额出口，
    // 仍然走同一个 `escapeHtml`（两列一视同仁，不靠"这个字段应该安全"的推断）。
    expect(cell(12, { ...ROW, taker_name: '<img src=x onerror=alert(1)>' })).toBe(
      '&lt;img src=x onerror=alert(1)&gt;',
    )
    expect(cell(13, { ...ROW, taker_price: '"><b>1</b>' })).not.toContain('<b>')
  })

  it('用户输入被转义（`templet` 的返回值被 layui 当 HTML 插入）', () => {
    expect(escapeHtml('<img src=x onerror=alert(1)>')).toBe(
      '&lt;img src=x onerror=alert(1)&gt;',
    )
    expect(cell(7, { ...ROW, project_text: '<b>注入</b>' })).toBe('&lt;b&gt;注入&lt;/b&gt;')
  })

  it('订单号列的单号（后端口径，可能带引号）也被转义', () => {
    const html = cell(1, { ...ROW, order_no: '"><img src=x>' })
    expect(html).not.toContain('<img')
    expect(html).toContain('&quot;&gt;&lt;img src=x&gt;')
  })

  it('订单号列（下标 1）：加急订单渲染**本页**对标的 `#FFBBFF`「急」徽标（逐字取 L753）', () => {
    const html = cell(1, { ...ROW, is_urgent: true })
    // 期望值写字面值（E-0024）：下面四个串都来自 `myorderlist.js:753` / zh-CN 字典 L377-378，
    // 不自指任何常量；第一条把整颗徽标**逐字**钉住。
    expect(html).toContain(
      '<span class="lableb" style="background-color:#FFBBFF" title="加急订单，优先安排">急</span>',
    )
    expect(html).toContain('background-color:#FFBBFF')
    expect(html).toContain('title="加急订单，优先安排"')
    expect(html).toContain('>急</span>')
    expect(html).toContain('class="lableb"')
  })

  it('加急徽标的位置：复制 span **之后**、订单号蓝链 **之前**（对标 L763-766 的顺序）', () => {
    const html = cell(1, { ...ROW, is_urgent: true })
    const copySpan = html.indexOf('class="orderIDcopy"')
    const badge = html.indexOf('background-color:#FFBBFF')
    // 蓝链的 needle 用 `class="orderdetails"`：拆链接的 `<a>` 上第一个属性是 `href`
    //（对标 L764 逐字如此），`<a class=` 这个串根本不出现。
    const details = html.indexOf('class="orderdetails"')
    expect(copySpan).toBeGreaterThanOrEqual(0)
    expect(badge).toBeGreaterThan(copySpan)
    expect(details).toBeGreaterThan(badge)
  })

  it('订单号列：`is_urgent: false` ⇒ **不渲染**加急徽标（不靠额外口径）', () => {
    expect(cell(1, { ...ROW, is_urgent: false })).not.toContain('#FFBBFF')
    expect(cell(1)).not.toContain('#FFBBFF')
  })

  it('状态列（下标 9）：`.lableb.setdlsta` 方块徽标，文案取后端词表、色取本页色表', () => {
    const html = cell(9, { ...ROW, status: 'WAITING_GRAB' })
    expect(html).toContain('class="lableb setdlsta"')
    // `href="javascript:void(0)"` —— **无尾分号**（照抄对标）。
    expect(html).toContain('href="javascript:void(0)"')
    // 对标的 `data-id` 是 `res.orderId`（**订单主键**）；我们载荷没有它 ⇒ 填 `order_no`
    // （偏差登记在文件头第 9 条）；`data-order` 是本页点击委托用的 `order_no`。
    expect(html).toContain(`data-id="${ROW.order_no}"`)
    expect(html).toContain('data-order="20260920213215233029"')
    expect(html).toContain('>待抢单</a>')
    expect(html).toContain('background-color:#585858;')
    // 业务方规则：同一个状态在任何页面同一文案（对标这一页写「待上号」，我们不复刻那个口径）
    expect(html).not.toContain('待上号')
  })

  it('状态列：`COMPLETED` 取本页色表的 `#0094ff`', () => {
    expect(cell(9, { ...ROW, status: 'COMPLETED' })).toContain('background-color:#0094ff;')
  })

  it('状态列：未知取值 → 原样文案 + 对标那个**带尾分号**的默认色（不做静默丢弃）', () => {
    const html = cell(9, { ...ROW, status: 'WEIRD' })
    expect(html).toContain('>WEIRD</a>')
    expect(html).toContain('#b0aeae;')
  })

  it('状态列：未知状态串（后端给的）也被转义', () => {
    const html = cell(9, { ...ROW, status: '<img src=x>' })
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img src=x&gt;')
  })

  it('订单截图列（下标 14）：对标的两颗图标按钮（`upImg` / `mangeImg`）', () => {
    const html = cell(14)
    expect(html).toContain('layui-btn-group')
    expect(html).toContain('upImg')
    expect(html).toContain('mangeImg')
    expect(html).toContain('&#xe681;')
    expect(html).toContain('&#xe64a;')
    expect(html).toContain('title="上传截图"')
    expect(html).toContain('title="查看截图"')
    // 对标的 `data-id` 是 `res.orderId`（**订单主键**）；我们载荷没有它 ⇒ 填 `order_no`
    // 我们载荷没有订单主键 ⇒ 填 `order_no`（偏差登记在文件头第 9 条）。
    expect(html).toContain(`data-id="${ROW.order_no}"`)
    expect(html).toContain('layui-btn-xs layui-btn-normal')
  })

  it('接手时间（下标 15）：格式化成 `YYYY-MM-DD HH:mm`（与大厅/管理列表同口径）+ 对标的 title', () => {
    // 期望值写字面值（不用 ROW.created_at 自指，否则改坏了不会红）；ROW 的 ISO 串是
    // `2026-09-20T21:32:15+08:00`，本页应与 `order-orderlist.ts` 一样渲染成 `2026-09-20 21:32`。
    expect(cell(15)).toBe('<span title="订单接手时间">2026-09-20 21:32</span>')
  })

  it('下标 15（接手时间）的 field 换成我们的 created_at，否则点排序看似无反应', () => {
    expect(cols[15]!.field).toBe('created_at')
    expect(cols[15]!.sort).toBe(true)
  })
})

describe('本页状态色表（按**状态枚举**；不与 order-orderlist 的那张合并，§10.3）', () => {
  it('7 档色值逐条取本页对标 switch（`myorderlist.js` L815-845）', () => {
    expect(STATUS_BADGE_COLORS).toEqual({
      WAITING_GRAB: '#585858', // 对标 orderStaus2「待上号」那一档
      IN_PROGRESS: '#219227', // orderStaus3「代练中」
      PENDING_REVIEW: '#ff6a00', // orderStaus4「待验收」
      COMPLETED: '#0094ff', // orderStaus5「已完成」
      ISSUE: '#FF5722', // orderStaus6「问题单」
      REFUNDED: '#ff0000', // orderStaus7「已退单」
      PAUSED: '#efaaaa', // orderStaus8「已暂停」
    })
  })

  it('`UNASSIGNED`（未分配）不在表里 → 走对标 switch 的 default（带尾分号）', () => {
    expect(STATUS_BADGE_COLORS['UNASSIGNED']).toBeUndefined()
    expect(STATUS_BADGE_FALLBACK).toBe('#b0aeae;')
  })
})

describe('提交完单：真的走 POST /api/business/orders/<no>/submit/', () => {
  it('先 layer.confirm（对标 L1200 的确认文案），确认后 POST 单号在 URL 里、不带请求体', async () => {
    await mountPage()
    const opts = clickSubmit()
    expect(opts.text).toBe(SUBMIT_CONFIRM_TEXT)
    expect(LAYUI.close).toHaveBeenCalledWith(0)
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenCalledWith({
        method: 'post',
        url: '/api/business/orders/20260920213215233029/submit/',
      })
    })
  })

  it('成功后刷新列表 + 成功提示（顺序：先 reload 再 msg）', async () => {
    await mountPage()
    clickSubmit()
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(SUBMIT_SUCCESS_TEXT, { time: 1500 })
    })
    expect(LAYUI.reload).toHaveBeenCalledWith(
      'tableList',
      expect.objectContaining({ page: { curr: 1, limit: 20 } }),
    )
  })

  it('「不是你接的单」（HTTP 403）→ msg 是后端原文，且**不显示成功文案**', async () => {
    await mountPage()
    mockedRequest.mockImplementation((async (config: { method?: string }) => {
      if (config.method === 'post') throw new HttpError(403, '该单不是你接的，不能提交')
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }) as never)
    clickSubmit()
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('该单不是你接的，不能提交', { time: 6000 })
    })
    expect(LAYUI.msg).not.toHaveBeenCalledWith(SUBMIT_SUCCESS_TEXT, expect.anything())
  })

  it('「只有代练中的单可以提交，当前状态：待验收」（HTTP 200 + code 1002）也原样显示', async () => {
    await mountPage()
    mockedRequest.mockImplementation((async (config: { method?: string }) => {
      if (config.method === 'post') {
        throw new BizError(1002, '只有代练中的单可以提交，当前状态：待验收')
      }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }) as never)
    clickSubmit()
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('只有代练中的单可以提交，当前状态：待验收', {
        time: 6000,
      })
    })
  })
})

describe('仍然「尚未开放」的落点（按新口径重写旧断言，不删）', () => {
  it('工具栏「刷新列表」是真动作：重新发一次 GET', async () => {
    await mountPage()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'refresh' })
    await vi.waitFor(() => expect(LAYUI.reload).toHaveBeenCalledTimes(1))
  })

  it('工具栏**只剩 1 颗**提示「尚未开放」（文案取**证据件**里的按钮原文）', async () => {
    // ⭐ 「发到我的管理系统」（`sendToMySystem`）**已接真**（见下面的手动推送用例）
    // ⇒ 这张白名单只剩 `checkOrderByLocal`（后端确实没有这个端点）。
    await mountPage()
    const handler = LAYUI.tableHandlers.get('toolbar(tableList)')
    LAYUI.msg.mockClear()
    handler?.({ event: 'checkOrderByLocal' })
    expect(LAYUI.msg).toHaveBeenCalledWith('「检查发送失败订单」尚未开放', { time: 1500 })
    // 负向对照：已接真的那颗**不许**再弹「尚未开放」（弹了就是把真功能说成没做）。
    LAYUI.msg.mockClear()
    handler?.({ event: 'sendToMySystem' })
    expect(LAYUI.msg).not.toHaveBeenCalledWith(
      '「发到我的管理系统」尚未开放',
      expect.anything(),
    )
  })

  it('⛔ 被删的两颗（`setRecMan` / `setJiWei`）**连标记带事件一起没了**（半新半旧会红）', () => {
    // 「按钮删了、事件还留在白名单里」是**半新半旧**：真 layui 发不出这两个事件，所以它不会
    // 表现为「点了没反应」，而是「将来有人把按钮加回来时又被那句话挡住」⇒ 两侧一起钉。
    // ⚠️ 判据搜的是**被废除的主词本身**（`AGENTS.md` §5-C 的最后一条），不是它派生出的短语；
    //    且只搜**去注释后的标记**（文件头那段留痕会写出这两个名字）。
    expect(EVIDENCE_MARKUP).not.toContain('setRecMan')
    expect(EVIDENCE_MARKUP).not.toContain('setJiWei')
    expect(TOOLBAR_NOT_OPEN_EVENTS).not.toContain('setRecMan')
    expect(TOOLBAR_NOT_OPEN_EVENTS).not.toContain('setJiWei')
  })

  it('layui 内置「筛选列」图标（`LAYTABLE_COLS`）**不弹**「尚未开放」，也不发请求', async () => {
    // 本页 `defaultToolbar:['filter']` ⇒ 右上角那颗图标是**真功能**，layui 会用同一个
    // `toolbar(id)` 事件把它的点击交上来（事件名 `LAYTABLE_COLS`，见 `.tools/parity.mjs:255`）。
    // 对标那个 handler 的 switch **没有 default 分支** ⇒ 我们也不能用 `else` 兜底弹提示
    // （emp-myemp 有一处同形缺陷，别再复制过来）。
    await mountPage()
    const before = mockedRequest.mock.calls.length
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'LAYTABLE_COLS' })
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('「尚未开放」白名单恰好 1 个（不多不少）', () => {
    // ⚠️ 钉字面值（E-0024）：`sendToMySystem` 已接真 ⇒ **不许**再出现在这张表里。
    expect(TOOLBAR_NOT_OPEN_EVENTS).toEqual(['checkOrderByLocal'])
  })
})

describe('订单截图两颗按钮：各自原地开层（图片存储线 Task 8；不再是「尚未开放」）', () => {
  /** 把订单截图列（下标 14）的**真实 templet 输出**放进 `root`，再点真按钮。 */
  async function mountWithShotButtons(): Promise<HTMLElement> {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[14]!.templet(ROW)
    root.appendChild(wrap)
    return root
  }

  it('两颗按钮都在（`upImg` / `mangeImg`），且**不是** disabled/置灰', async () => {
    const root = await mountWithShotButtons()
    const up = root.querySelector<HTMLButtonElement>('button.upImg')
    const mange = root.querySelector<HTMLButtonElement>('button.mangeImg')
    expect(up).not.toBeNull()
    expect(mange).not.toBeNull()
    expect(up!.disabled).toBe(false)
    expect(mange!.disabled).toBe(false)
  })

  it('点「上传截图」/「查看截图」⇒ 各开一个真弹层，**参数逐项取本页自己的** `myorderlist.js:1432-1460`', async () => {
    const root = await mountWithShotButtons()
    const before = mockedRequest.mock.calls.length

    LAYUI.open.mockClear()
    root.querySelector<HTMLElement>('button.upImg .layui-icon')?.click()
    const up = LAYUI.open.mock.calls[0]?.[0] as Record<string, unknown>
    // 标题取自本页 i18n 的 zh-CN 原文（`myorderlist.js` L460）；⛔ 与按钮自己的 `title`
    // （「上传截图」，证据件里那颗 tooltip）**不是一个值**，别一起改。
    expect(up['title']).toBe('上传订单截图')
    expect(up['shade']).toEqual([0.3, '#333'])
    expect(up['shadeClose']).toBe(true)
    expect(up['type']).toBe(2)
    expect(up['skin']).toBe('layui-layer-lan')
    expect(up['anim']).toBe(8)
    // PC 分支（`device = isPc`，L680）：`[device ? '70%' : '90%', device ? '85%' : '90%']`
    expect(up['area']).toEqual(['70%', '85%'])
    // ⚠️ 本页这两支**没有** `maxmin` 键（L1435-1444 里没写）⇒ 断言它不在。
    expect(Object.keys(up)).not.toContain('maxmin')
    expect(String(up['content'])).toContain(
      `layui-island/page.html?page=order-screenshots-upload&order_no=${ROW.order_no}`,
    )

    LAYUI.open.mockClear()
    root.querySelector<HTMLElement>('button.mangeImg')!.click()
    const mange = LAYUI.open.mock.calls[0]?.[0] as Record<string, unknown>
    expect(mange['title']).toBe('查看订单截图')
    // ⚠️ 管理那一支的 `anim` 是 **1**（上传是 8）——两页/两支各不相同，⛔ 别抽共用常量。
    expect(mange['anim']).toBe(1)
    expect(mange['area']).toEqual(['80%', '85%'])
    expect(String(mange['content'])).toContain(
      `layui-island/page.html?page=order-screenshots-manage&order_no=${ROW.order_no}`,
    )

    // 开层是纯前端动作：**一个 API 请求都不发**（数据由层里的岛页自己取）。
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('单号形状不是 20 位数字 ⇒ **不开层**、只给固定文案（`title` 是 HTML 上下文）', async () => {
    const root = await mountPage()
    const wrap = document.createElement('div')
    // 行载荷的单号被换成一份「不可信形状」的值（`data-id` 会经 `dataset` 解码回 `<`）
    wrap.innerHTML = buildCols()[14]!.templet({ ...ROW, order_no: 'x"><img onerror=1>' })
    root.appendChild(wrap)
    LAYUI.open.mockClear()
    root.querySelector<HTMLElement>('button.upImg')!.click()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('订单号格式不正确，无法打开订单截图', { time: 1500 })
  })
})

/**
 * 权限门：内置「代练员」没有 `order` 栏目 ⇒ 它点「查看截图」
 * 会打 `GET …/screenshots` 吃 403（职位对拍 `findings` 当场红）。本仓既定口径是
 * 「**没权限的不渲染**」（先例 `index-console.ts::visibleShortcuts`），⛔ 不是置灰、更不是摆着让它 403。
 */
describe('订单截图权限门：没有节点就不渲染', () => {
  /** 用指定的 `/api/auth/me` 载荷挂页，返回订单截图列（下标 14）的 templet 输出。 */
  async function cellWithMe(me: unknown): Promise<string> {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return me
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    await mountPage(impl)
    return buildCols()[14]!.templet(ROW)
  }

  it('代练员（两个节点都没有）⇒ 两颗都不渲染', async () => {
    const html = await cellWithMe({ user: { menu_permissions: [] } })
    expect(html).toBe('')
  })

  it('只有 `order.view_screenshot` ⇒ 只渲染「查看截图」那一颗', async () => {
    const html = await cellWithMe({ user: { menu_permissions: ['order.view_screenshot'] } })
    expect(html).toContain('mangeImg')
    expect(html).not.toContain('upImg')
  })

  it('只有 `order.upload_screenshot` ⇒ 只渲染「上传截图」那一颗', async () => {
    const html = await cellWithMe({ user: { menu_permissions: ['order.upload_screenshot'] } })
    expect(html).toContain('upImg')
    expect(html).not.toContain('mangeImg')
  })

  it('身份取不到（`/api/auth/me` 抛错）⇒ fail-closed：两颗都不渲染', async () => {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') throw new Error('401')
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    await mountPage(impl)
    expect(buildCols()[14]!.templet(ROW)).toBe('')
  })

  it('租户管理员短路 ⇒ 两颗都在（与壳层 `session.canNode` 同口径）', async () => {
    const html = await cellWithMe({ user: { is_tenant_admin: true, menu_permissions: [] } })
    expect(html).toContain('upImg')
    expect(html).toContain('mangeImg')
  })

  it('两个节点都有（默认夹具）⇒ 两颗都在，且顺序仍是 `upImg` → `mangeImg`（对标 L902/L903）', async () => {
    const html = await cellWithMe(ME_FULL)
    expect(html.indexOf('upImg')).toBeLessThan(html.indexOf('mangeImg'))
  })
})

describe('`#total` 统计句里的状态词', () => {
  it('写「待抢单0笔」而不是对标的「待上号0笔」（有意偏差，见文件头登记）', async () => {
    const root = await mountPage()
    // jsdom 里没有真 layui，所以手动跑 `table.render` 的 `done`（`renderTotal` 就在那里被调）。
    const opts = LAYUI.render.mock.calls[0]?.[0] as { done?: () => void }
    opts.done?.()
    const total = root.querySelector('#total')
    expect(total).not.toBeNull()
    expect(total!.textContent).toContain('其中待抢单0笔')
    expect(total!.textContent).not.toContain('待上号')
  })
})

describe('行内两处链接接到真实出口（修复轮 1：原先点了没反应）', () => {
  /** 把某一列的**真实 templet 输出**放进 `root`，再点里面的真元素。 */
  async function mountWithCell(colIndex: number): Promise<HTMLElement> {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[colIndex]!.templet(ROW)
    root.appendChild(wrap)
    return root
  }

  /* ═══════════ 点单号 ⇒ 原地开层 ═══════════ */

  /**
   * 点那一列的真蓝链，返回 `layer.open` 的入参。
   *
   * @param orderNo 该行载荷里的 `order_no`（决定 `content` 与 `data-viewId`）。
   * @param linkViewId 覆盖链接上的 `data-viewId`（**只**用来钉「title 前缀 = viewID」这条；
   *   生产代码里 templet 把两个位置都填 `order_no`，见文件头第 9 条）。
   */
  async function clickOrderLink(
    orderNo: string = ROW.order_no,
    linkViewId?: string,
  ): Promise<Record<string, unknown>> {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[1]!.templet({ ...ROW, order_no: orderNo })
    root.appendChild(wrap)
    const link = wrap.querySelector<HTMLElement>('a.orderdetails')
    expect(link, '订单号 templet 没有产出 a.orderdetails').not.toBeNull()
    if (linkViewId !== undefined) link!.setAttribute('data-viewid', linkViewId)
    // 首屏取数发生在挂载期，与「开层」无关 ⇒ 先清一次，再看点击这一下。
    LAYUI.open.mockClear()
    navigate.mockClear()
    link!.click()
    expect(LAYUI.open, '点了单号蓝链没有开层').toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('⭐ 层参数逐字（title / shade / shadeClose / maxmin:false / type / skin / anim / area = **本页 PC 分支的** `85%×92%`）', async () => {
    const opts = await clickOrderLink()
    // 对标：`title: viewID + i18nDataList.viewOrderDetails`，zh-CN 的 `viewOrderDetails`
    // = 「订单详情」（`myorderlist.js` L456，原文抄在上面）。
    expect(opts['title']).toBe('20260920213215233029订单详情')
    expect(opts['shade']).toEqual([0.3, '#333'])
    expect(opts['shadeClose']).toBe(true)
    // ⚠️ PC 分支显式 `maxmin: false`（L1330）；手机分支才是 `true`（L1340）。
    expect(opts['maxmin']).toBe(false)
    expect(opts['type']).toBe(2)
    expect(opts['skin']).toBe('layui-layer-lan')
    expect(opts['anim']).toBe(8)
    // ⛔ **本页自己的值**（L1333）。改成手机分支的 `['95%','95%']` 或任何别页的值，本条必须红。
    expect(opts['area']).toEqual(['85%', '92%'])
  })

  it('⭐ `content` = 我们自己的复刻页（`page=order-orderview` + `order_no` = 那一行的单号），且**不再跳路由**', async () => {
    const opts = await clickOrderLink()
    // 尖括号里的 `<BASE_URL>` 在本工程是 `/`（vitest 的 base 与 dev 相同）。
    expect(opts['content']).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
    )
    // 换个单号 → `content` 跟着**行**走（不是写死的第一个值）。
    const opts2 = await clickOrderLink('20260101010101010101')
    expect(opts2['content']).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260101010101010101',
    )
    // 负向对照：改了之后**一次都没有**跳路由（`ctx.onNavigate` 是 T5 之前那条路）。
    expect(navigate).not.toHaveBeenCalled()
  })

  it('⭐ 基址用 `import.meta.env.BASE_URL`（⛔ 不写死 `/`）：把它 stub 成 `/sub/` ⇒ `content` 跟着变', async () => {
    // ⚠️ **先挂载、再 stub**：挂载期取证据件的 `fetch` 也走 `BASE_URL`，提前改会让那条路径取错文件。
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[1]!.templet(ROW)
    root.appendChild(wrap)
    const link = wrap.querySelector<HTMLElement>('a.orderdetails')!
    vi.stubEnv('BASE_URL', '/sub/')
    try {
      LAYUI.open.mockClear()
      link.click()
      // 期望值是**字面值**（E-0024）⇒ **不是自指断言**：把实现的 `${import.meta.env.BASE_URL}`
      // 写死成 `'/'`，本条会红（见收口记录的变异 ⑪）。
      expect((LAYUI.open.mock.calls[0]![0] as Record<string, unknown>)['content']).toBe(
        '/sub/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('⭐ `title` 的前缀是链接的 `data-viewId`（对标 `viewID`），不是写死的那个单号', async () => {
    const opts = await clickOrderLink(ROW.order_no, 'TB-777')
    expect(opts['title']).toBe('TB-777订单详情')
  })

  it('⭐ `title` 是 HTML 上下文 ⇒ 前缀那一道 `escapeHtml` **才是承重的**（`dataset` 会把实体解码回 `<`）', async () => {
    // 夹具 = 带标记的显示单号（`data-viewId` 就是 title 前缀的来源）。⚠️ `order_no` 仍是
    // 20 位数字 ⇒ 形状闸放行，这一条测的是 `title` 那道转义、不是形状闸。
    const opts = await clickOrderLink(ROW.order_no, '<b>x</b>&"')
    // 期望值写字面值（E-0024）：`<` `>` `&` `"` 四个都换掉，末尾接中文「订单详情」。
    expect(opts['title']).toBe('&lt;b&gt;x&lt;/b&gt;&amp;&quot;订单详情')
    // 反例：**未转义**那份一个都不许在。理由：`link.dataset.*` 会把 attribute 实体解码回
    // `<`/`>`，而 vendored `layui.js` 是把 `a.title` **字符串拼进**
    // `<div class="layui-layer-title">' + a.title + '</div>` 的 ⇒ 到了 `layer.open` 这一层，
    // 只有**在传参前**再过一次 `escapeHtml` 才拦得住（templet 里那次已被 `dataset` 解掉）。
    expect(opts['title']).not.toContain('<b>')
  })

  it('⭐ 开层**不发任何 API 请求**（数据由层里的 island 页自己取；本页只开层）', async () => {
    // ⚠️ 必须**先挂载完**再量：`mountPage()` 自己会发首屏取数——把 `before` 量在挂载之前，
    //    会把「挂载的请求」算到「点击」头上（本用例第一版就是这么假红的）。
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[1]!.templet(ROW)
    root.appendChild(wrap)
    const link = root.querySelector<HTMLElement>('a.orderdetails')!
    const before = mockedRequest.mock.calls.length
    link.click()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('⛔ 开层前的**单号形状闸**：非 20 位数字 / 带标记的 `data-order` ⇒ **不开层** + 固定文案提示 + **零请求**', async () => {
    const root = await mountWithCell(1)
    const link = root.querySelector<HTMLElement>('a.orderdetails')!
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
    // 拒绝分支也**不许发请求**（与放行分支同口径）：量的是四次点击前后的差。
    const before = mockedRequest.mock.calls.length
    for (const value of badShapes) {
      link.setAttribute('data-order', value)
      link.click()
    }
    expect(LAYUI.open, '形状不对却开了层').not.toHaveBeenCalled()
    expect(mockedRequest.mock.calls.length, '拒绝分支发了请求').toBe(before)
    // 提示是**固定文案**（⛔ 不把那份不可信的输入拼进来）；四次点击 = 四条提示。
    expect(LAYUI.msg).toHaveBeenCalledWith('订单号格式不正确，无法打开订单详情', { time: 1500 })
    expect(LAYUI.msg).toHaveBeenCalledTimes(badShapes.length)
  })

  it('⭐ 2026-10-29（fix/taker-web-order-detail）：纯打手节点集（无 view_detail/view_content）蓝链照样开层，层 URL **不带任何模式标志**（受限判定在 order-orderview 页内自判）', async () => {
    // 节点集 = 纯打手：只有「我的订单」入口那颗 + 提交完单那颗——两颗详情节点都不在
    //（打手点开详情会进 order-orderview 的**受限模式**，那是那一页自己的判据，
    //  本页不知道也不需要知道）。
    const takerImpl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') {
        return {
          user: {
            is_tenant_admin: false,
            is_account_admin: false,
            menu_permissions: ['my_orders.send_to_my_system', CHANGE_STATUS_NODE_CODE],
          },
        }
      }
      return defaultImpl([ROW])(config)
    }
    const root = await mountPage(takerImpl)
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[1]!.templet(ROW)
    root.appendChild(wrap)
    const link = wrap.querySelector<HTMLElement>('a.orderdetails')
    expect(link, '纯打手节点集下蓝链也要渲染（入口不藏）').not.toBeNull()
    LAYUI.open.mockClear()
    link!.click()
    expect(LAYUI.open, '无详情节点账号点蓝链也要开层').toHaveBeenCalledTimes(1)
    const opts = LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
    // 层 URL 与既有那条「content 逐字」断言**同一个值**：page=order-orderview + order_no，
    // 且没有任何 mode / restricted 之类的标志——打手有没有详情节点由层里的岛页自己探
    // `/api/auth/me` 判（先例：`@/layui-island/perms` 的裸 fetch 探针）。
    expect(opts['content']).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
    )
    expect(String(opts['content'])).not.toMatch(/mode|restricted|limited|view=/i)
    // ⚠️ 本文件的 beforeEach 不清 `LAYUI.open`（既有用例各自在点前清）⇒ 这里收尾清一次，
    //    不然这一条会渗进下一条「状态徽标」用例的 calls[0]。
    LAYUI.open.mockClear()
  })

  it('点状态徽标 `.setdlsta` → 开「更改状态」弹层（⭐ 2026-09-28 起不再是「尚未开放」）', async () => {
    const root = await mountWithCell(9)
    const badge = root.querySelector<HTMLElement>('a.setdlsta')
    expect(badge).not.toBeNull()
    const before = mockedRequest.mock.calls.length
    badge!.click()
    // 只开层、**一个业务请求都不发**（真正发请求要等用户在层里点「确定」）。
    expect(Object.keys(LAYUI.open.mock.calls[0]?.[0] as object)).toContain('btn')
    expect(mockedRequest.mock.calls.length).toBe(before)
    expect(LAYUI.msg).not.toHaveBeenCalledWith('「更改订单状态」尚未开放', { time: 1500 })
  })

  it('右键 `.orderLine` → 提示「右键菜单」尚未开放 + `preventDefault`（对标那个菜单我们没实现）', async () => {
    const root = await mountWithCell(1)
    LAYUI.msg.mockClear()
    const line = root.querySelector<HTMLElement>('.orderLine')
    expect(line).not.toBeNull()
    const before = mockedRequest.mock.calls.length
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true })
    line!.dispatchEvent(event)
    expect(LAYUI.msg).toHaveBeenCalledWith('「右键菜单」尚未开放', { time: 1500 })
    expect(event.defaultPrevented).toBe(true)
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('右键 `.orderLine` 之外 → 不提示、也不 `preventDefault`（原生菜单照旧）', async () => {
    const root = await mountWithCell(1)
    LAYUI.msg.mockClear()
    const outside = document.createElement('div')
    root.appendChild(outside)
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true })
    outside.dispatchEvent(event)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
  })
})

describe('证据件「订单状态」筛选下拉：文案与 value 都是「待抢单」（有意偏差，见文件头登记）', () => {
  it('第一档是 `<option value="待抢单">待抢单</option>`，不含对标的「待上号」', () => {
    expect(STA_SELECT).not.toBe('')
    expect(STA_SELECT).toContain('<option value="待抢单">待抢单</option>')
    expect(STA_SELECT).not.toContain('待上号')
  })
})

/**
 * ⭐ 工具栏多出来的那颗「**同步订单状态**」（spec §9.3）。
 *
 * ⚠️ 它是一颗**新**按钮（对标那一排里**没有**它）⇒ 这是**可见偏离**，登记在
 * `order-myorderlist.ts` 文件头的「已知会不同」表与 `docs/FRONTEND-REPLICA-STANDARD.md` §7。
 * ⛔ 对标本来就有的「发到我的管理系统」（`sendToMySystem`）**一个字都不许动**：
 * 它仍然是「尚未开放」（下面第一条就是这条对照）。
 *
 * 驱动方式：真 layui 在 jsdom 里加载不了（`bootstrapIsland` 是替身），所以点击落点由
 * **layui 交上来的那个 `toolbar(id)` 事件**驱动——它就是真实浏览器里点那颗按钮的**唯一**入口
 * （`table.on('toolbar(tableList)')`）。⛔ 不写成「DOM 里有这颗按钮就算过」的空断言。
 */
describe('工具栏「同步订单状态」（下游鸡蛋铺线 Task 9）', () => {
  /** 页面**真的**注入到工具栏模板里的那段 HTML（layui 编译的就是它）。 */
  const toolbarTemplate = (root: HTMLElement): string =>
    root.querySelector<HTMLScriptElement>('#toolbarDemo')?.textContent ?? ''

  /**
   * 页面真的注入的那颗按钮。
   * ⚠️ 用**类名**找它（类名是标记的一部分），`lay-event` 也是从**标记**里读的
   * ——⛔ 不用生产那个 `SYNC_TOOLBAR_EVENT` 常量去找/去发事件：那样是**自指**判据
   * （常量改名时测试与实现一起变、照样绿，而真实浏览器里按钮发的是**标记里那个**事件名）。
   * 实测：把标记里的 `lay-event` 写死成另一个串时，只有这条链能红（见报告里的变异 M1）。
   */
  function realSyncButton(root: HTMLElement): HTMLButtonElement {
    const holder = document.createElement('div')
    holder.innerHTML = toolbarTemplate(root)
    const btn = holder.querySelector<HTMLButtonElement>(`button.${SYNC_TOOLBAR_BUTTON_CLASS}`)
    expect(btn, '工具栏模板里没有那颗新按钮').not.toBeNull()
    return btn!
  }

  /**
   * 真的点一下那颗按钮：真 layui 在 jsdom 里加载不了，但它在真实浏览器里交上来的**就是**
   * 标记上那个 `lay-event`（`table.on('toolbar(tableList)')`）⇒ 这样驱动才等价于真点击。
   */
  function clickSyncButton(root: HTMLElement): void {
    const event = realSyncButton(root).getAttribute('lay-event') ?? ''
    expect(event, '那颗按钮没有 `lay-event`').not.toBe('')
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event })
  }

  it('按钮真的进了工具栏模板：文案 =「同步订单状态」、带自己的 `lay-event`', async () => {
    const root = await mountPage()
    const template = toolbarTemplate(root)
    // 非空哨兵：模板取不到时下面的 `querySelector` 恒 `null` ⇒ 判据会变成恒假（红）而不是恒真，
    // 但这里还是显式钉一下，失败信息更直白。
    expect(template).not.toBe('')
    const holder = document.createElement('div')
    holder.innerHTML = template
    const btn = holder.querySelector<HTMLButtonElement>(`button[lay-event="${SYNC_TOOLBAR_EVENT}"]`)
    expect(btn, '工具栏模板里没有那颗新按钮').not.toBeNull()
    expect(btn!.textContent).toContain(SYNC_TOOLBAR_BUTTON_LABEL)
    expect(SYNC_TOOLBAR_BUTTON_LABEL).toBe('同步订单状态')
    // ⛔ **钉字面值**（E-0024）：上面那句 `querySelector` 用的是生产常量 ⇒ 单独看它是**自指**的
    // （常量改名时两边一起变、照样绿）。这里把「模板里真的写着这个名字」钉死：
    // 把它改成别的串（而 handler 还在等旧名）＝「按钮点了没反应」那种静默失败，本行会红。
    expect(template).toContain(`lay-event="${SYNC_TOOLBAR_EVENT}"`)
    expect(SYNC_TOOLBAR_EVENT).toBe('syncOrderStatus')
    // ⛔ **E-0061 陷阱①**：类名与选择器分成两个常量（`class="${SELECTOR}"` 会造出 `class=".x"`）。
    expect(SYNC_TOOLBAR_BUTTON_CLASS.startsWith('.')).toBe(false)
    expect(btn!.classList.contains(SYNC_TOOLBAR_BUTTON_CLASS)).toBe(true)
    expect(btn!.getAttribute('type')).toBe('button')
    // 负向对照：对标那颗「发到我的管理系统」**还在**（标记没动），而且它**不再**在
    // 「尚未开放」名单里 —— 它自己已接真（只是与「同步」是**两个不同动作**）。
    const legacy = holder.querySelector<HTMLButtonElement>('button[lay-event="sendToMySystem"]')
    expect(legacy, '对标的「发到我的管理系统」被动了').not.toBeNull()
    expect(legacy!.textContent).toContain('发到我的管理系统')
    expect(TOOLBAR_NOT_OPEN_EVENTS).not.toContain('sendToMySystem')
    expect(TOOLBAR_NOT_OPEN_EVENTS).not.toContain(SYNC_TOOLBAR_EVENT)
  })

  it('点它 ⇒ 发 POST `/api/business/member/jidanpu-sync-now/`（**空体**），成功提示带本次笔数 + 刷新列表', async () => {
    const root = await mountPage()
    const before = mockedRequest.mock.calls.length
    LAYUI.reload.mockClear()
    LAYUI.msg.mockClear()
    clickSyncButton(root)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())

    const configs = mockedRequest.mock.calls.slice(before)
      .map((c) => c[0] as ReqConfig & { data?: unknown })
    // 点这一下只发两条：**立即同步**（POST、空体）与**刷新列表**（GET）。
    // 判据：动词 + 路径字面值 + **没有请求体**（行主只来自登录态，越权面 = 0）。
    expect(configs.map((c) => `${c.method} ${c.url}`)).toEqual([
      'post /api/business/member/jidanpu-sync-now/',
      'get /api/business/my-orders/',
    ])
    expect(configs[0]!.data).toBeUndefined()
    // 成功文案是**自拟**的（后端成功信封的 `msg` 是空串，照抄就什么都不显示），
    // 但它必须把「这一轮更新了几笔」说出来——否则用户看不出同步到底干了什么。
    expect(LAYUI.msg).toHaveBeenCalledWith('同步完成，本次更新 2 笔订单', { time: 1500 })
    // 同步会真的改我方订单状态 ⇒ 列表要重取一次，否则页面显示的还是旧状态。
    expect(LAYUI.reload).toHaveBeenCalled()
    // 那颗按钮**不在**「尚未开放」那条路上。
    expect(LAYUI.msg).not.toHaveBeenCalledWith(
      `「${SYNC_TOOLBAR_BUTTON_LABEL}」尚未开放`,
      expect.anything(),
    )
  })

  it('被频控挡下（后端回人话 + 剩余秒数）⇒ **原样显示**它（6000ms），⛔ 不自己再写一套节流判据', async () => {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return ME_FULL
      if (config.url === '/api/business/member/jidanpu-sync-now/') {
        // 频控的拒绝是**业务失败码**（HTTP 仍 200，spec §9.3）⇒ 走 `BizError`。
        throw new BizError(1000, '同步太频繁，请 2 秒后再试')
      }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    LAYUI.msg.mockClear()
    LAYUI.reload.mockClear()
    clickSyncButton(root)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())

    // 后端那句**带剩余秒数**的话必须原样上屏（自己写一句「操作太频繁」就等于把秒数吃掉了）。
    expect(LAYUI.msg).toHaveBeenCalledWith('同步太频繁，请 2 秒后再试', { time: 6000 })
    expect(LAYUI.msg).not.toHaveBeenCalledWith('同步完成，本次更新 2 笔订单', { time: 1500 })
    // 失败时**不**刷新列表（没改任何东西，白打一次请求）。
    expect(LAYUI.reload).not.toHaveBeenCalled()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════════════════
 * ⭐ 手动推送到我的下家（计划 `2026-10-23-taker-my-orders-push.md` §2 / §4）
 *
 * 两颗按钮、三个落点：
 *  - 「发到我的管理系统」（证据件里本来就有那颗）⇒ 勾选的行 `action: 'record'`；
 *  - 「同步订单状态」**有勾选** ⇒ 勾选的行 `action: 'status'`；
 *  - 「同步订单状态」**无勾选** ⇒ **保留原行为**（`jidanpu-sync-now` 拉一轮，⛔ 不许丢）。
 * ⚠️ 驱动方式与上面那颗同步按钮**同一条纪律**：从**标记**里读事件名去驱动 handler
 *（⛔ 不用生产常量找/发事件，那是自指判据），而勾选状态由 layui 的 `checkStatus` 替身给。
 * ═══════════════════════════════════════════════════════════════════════════════════════ */

describe('手动推送：勾选的行 ⇒ push-now（record / status）；未勾选 ⇒ 提示或拉取', () => {
  /** 页面真的注入到工具栏模板里的那段 HTML。 */
  const toolbarTemplate = (root: HTMLElement): string =>
    root.querySelector<HTMLScriptElement>('#toolbarDemo')?.textContent ?? ''

  /**
   * 从**标记**里取某个 `lay-event` 的按钮（⛔ 不用生产常量替代：那样「标记里的名字」写错了
   * 也照样能驱动 handler，等于把「按钮点了没反应」那种静默失败放过）。
   */
  function buttonOf(root: HTMLElement, event: string): HTMLButtonElement {
    const holder = document.createElement('div')
    holder.innerHTML = toolbarTemplate(root)
    const btn = holder.querySelector<HTMLButtonElement>(`button[lay-event="${event}"]`)
    expect(btn, `工具栏模板里没有 lay-event="${event}" 那颗按钮`).not.toBeNull()
    return btn!
  }

  /** 真的点一下：把标记里那个事件名交给 layui 交上来的 `toolbar(tableList)`。 */
  function clickToolbarButton(root: HTMLElement, event: string): void {
    const btn = buttonOf(root, event)
    const name = btn.getAttribute('lay-event') ?? ''
    expect(name, '那颗按钮没有 `lay-event`').toBe(event)
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: name })
  }

  /** 让 layui 的 `checkStatus` 回这一批行（默认夹具是空数组 = 未勾选）。 */
  function checkRows(rows: MyOrderItem[]): void {
    LAYUI.checkStatus.mockReturnValue({ data: rows })
  }

  /** 后端那次 push-now 的入参（动词 + 路径字面值 + 请求体）。 */
  function pushConfig(): ReqConfig & { data?: unknown } {
    const config = mockedRequest.mock.calls
      .map((call) => call[0] as ReqConfig & { data?: unknown })
      .find((row) => row.url === '/api/business/member/jidanpu-push-now/')
    expect(config, '没有发出 push-now 请求').toBeDefined()
    return config!
  }

  it('「发到我的管理系统」**接真**：勾选两行 ⇒ POST push-now，`action: record`、单号按勾选顺序', async () => {
    const root = await mountPage()
    const second: MyOrderItem = { ...ROW, id: 9, order_no: '20260920213215233030' }
    checkRows([ROW, second])
    mockedRequest.mockClear()
    LAYUI.reload.mockClear()
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'sendToMySystem')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())

    const config = pushConfig()
    expect(config.method).toBe('post')
    // 键名是后端序列化器的字段名（`order_nos` / `action`），⛔ 不改写成驼峰。
    expect(config.data).toEqual({
      order_nos: [ROW.order_no, second.order_no],
      action: 'record',
    })
    // 成功提示：**后端计数**说话（自拟文案；后端成功信封 msg 是空串）。
    // ⚠️ 计数取**后端回的**（默认替身给 `pushed:1 / skipped:1`），⛔ 不是前端自己数两行。
    expect(LAYUI.msg).toHaveBeenCalledWith('已推送 1 笔，跳过 1 笔', { time: 1500 })
    // 推完刷新列表（录单会改变对方系统，我方列表也要重取一遍）。
    expect(LAYUI.reload).toHaveBeenCalled()
    // ⛔ 不许再走「尚未开放」那条路。
    expect(LAYUI.msg).not.toHaveBeenCalledWith(
      '「发到我的管理系统」尚未开放',
      expect.anything(),
    )
  })

  it('未勾选 ⇒ **只提示**「请选中要操作的记录」，⛔ 不发任何请求', async () => {
    const root = await mountPage()
    checkRows([])
    mockedRequest.mockClear()
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'sendToMySystem')
    // 提示是同步弹的（`doPushToMySystem` 在第一个 await 之前就 return 了）。
    expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录', { time: 1500 })
    expect(mockedRequest).not.toHaveBeenCalled()
  })

  it('「同步订单状态」**有勾选** ⇒ 推状态（`action: status`），不是拉取', async () => {
    const root = await mountPage()
    checkRows([ROW])
    mockedRequest.mockClear()
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'syncOrderStatus')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())

    expect(pushConfig().data).toEqual({ order_nos: [ROW.order_no], action: 'status' })
    // ⛔ 这一档**不许**再打「拉一轮」那个端点（方向相反：一个往外推、一个往回收）。
    const urls = mockedRequest.mock.calls.map((c) => (c[0] as ReqConfig).url)
    expect(urls).not.toContain('/api/business/member/jidanpu-sync-now/')
  })

  it('「同步订单状态」**无勾选** ⇒ 仍走**拉取**（`jidanpu-sync-now`），⛔ 不发 push-now', async () => {
    const root = await mountPage()
    checkRows([])
    mockedRequest.mockClear()
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'syncOrderStatus')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())

    const calls = mockedRequest.mock.calls.map((c) => c[0] as ReqConfig)
    // 保留既有能力：无勾选 ⇒ 拉一轮（空体 POST）+ 刷新列表。
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'post /api/business/member/jidanpu-sync-now/',
      'get /api/business/my-orders/',
    ])
    expect(calls.some((c) => c.url === '/api/business/member/jidanpu-push-now/')).toBe(false)
    expect(LAYUI.msg).toHaveBeenCalledWith('同步完成，本次更新 2 笔订单', { time: 1500 })
  })

  it('失败（无绑定 1002）⇒ 后端 `msg` **原样**显示（6000ms），⛔ 不写成「操作失败」', async () => {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return ME_FULL
      if (config.url === '/api/business/member/jidanpu-push-now/') {
        throw new BizError(1002, '还没绑定或没启用下家鸡蛋铺，请到「我的资料 → 功能设置」里绑定')
      }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    checkRows([ROW])
    LAYUI.msg.mockClear()
    LAYUI.reload.mockClear()
    clickToolbarButton(root, 'sendToMySystem')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '还没绑定或没启用下家鸡蛋铺，请到「我的资料 → 功能设置」里绑定',
      { time: 6000 },
    )
    // 失败时不刷新（没改任何东西）。
    expect(LAYUI.reload).not.toHaveBeenCalled()
  })

  it('有 `failed` 时提示里**必须**出现失败笔数（⛔ 不许只报「推送 0 笔」）', async () => {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return ME_FULL
      if (config.url === '/api/business/member/jidanpu-push-now/') {
        return {
          requested: 3,
          pushed: 1,
          skipped: 1,
          failed: 1,
          items: [],
        }
      }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    checkRows([ROW])
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'sendToMySystem')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    expect(LAYUI.msg).toHaveBeenCalledWith('已推送 1 笔，跳过 1 笔，失败 1 笔', {
      time: 1500,
    })
  })

  it('跳过 / 失败时把**后端那句原因**缀在提示后面（修复轮 Important-2）', async () => {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return ME_FULL
      if (config.url === '/api/business/member/jidanpu-push-now/') {
        return {
          requested: 2,
          pushed: 0,
          skipped: 2,
          failed: 0,
          items: [
            { order_no: 'a', result: 'skipped', reason: '本线台账里没有这张单的链路' },
            { order_no: 'b', result: 'skipped', reason: '本线台账里没有这张单的链路' },
          ],
        }
      }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    checkRows([ROW])
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'sendToMySystem')
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    // ⚠️ 两条**相同**原因 ⇒ 不加「等」；原因文案是**后端原文**（前端不从状态码自造）。
    // ⚠️ 时长是 **6000ms**（复评 Minor-5：带原因时与 `errMsg` 同档，要来得及读完）。
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '已推送 0 笔，跳过 2 笔：本线台账里没有这张单的链路',
      { time: 6000 },
    )
  })

  it('🔒 请求还没回来时再点 ⇒ 提示「正在推送，请稍候」，⛔ 不发第二次', async () => {
    let release: (value: unknown) => void = () => {}
    const pending = new Promise<unknown>((resolve) => {
      release = resolve
    })
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return ME_FULL
      if (config.url === '/api/business/member/jidanpu-push-now/') return pending
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    const pushCalls = (): unknown[] =>
      mockedRequest.mock.calls.filter(
        (call) => (call[0] as ReqConfig).url === '/api/business/member/jidanpu-push-now/',
      )
    const root = await mountPage(impl)
    checkRows([ROW])
    mockedRequest.mockClear()
    LAYUI.msg.mockClear()
    clickToolbarButton(root, 'sendToMySystem')
    await vi.waitFor(() => expect(pushCalls()).toHaveLength(1))
    // 第二次点：只提示，不发第二次（真正的互斥在后端共享引擎的按绑定串行闸）。
    clickToolbarButton(root, 'sendToMySystem')
    expect(LAYUI.msg).toHaveBeenCalledWith('正在推送，请稍候', { time: 1500 })
    expect(pushCalls()).toHaveLength(1)
    // 收口：让第一次请求返回，正常提示（也证明 `pushing` 会复位）。
    release({ requested: 1, pushed: 1, skipped: 0, failed: 0, items: [] })
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('已推送 1 笔，跳过 0 笔', { time: 1500 }),
    )
  })
})

describe('手动推送的结果文案：**钉字面值**（修复轮 Important-2）', () => {
  it('跳过 / 失败时缀第一条**不同**原因；多条不同只在末尾加「等」', () => {
    // ⚠️ 钉字面值（E-0024）：这里测的是「怎么拼」，⛔ 不拿生产常量当期望值。
    expect(pushResultText(1, 1, 0, ['原因甲', '原因甲'])).toBe(
      '已推送 1 笔，跳过 1 笔：原因甲',
    )
    expect(pushResultText(1, 1, 1, ['原因甲', '原因乙', ''])).toBe(
      '已推送 1 笔，跳过 1 笔，失败 1 笔：原因甲等',
    )
    // 没有跳过也没有失败 ⇒ 不缀原因（`done` 那几单的 reason 是空串）。
    expect(pushResultText(2, 0, 0, [''])).toBe('已推送 2 笔，跳过 0 笔')
    // 全成功但传了原因也不缀：只有「跳过 / 失败 > 0」才说原因。
    expect(pushResultText(2, 0, 0, ['不该出现'])).toBe('已推送 2 笔，跳过 0 笔')
    expect(PUSH_IN_PROGRESS_TEXT).toBe('正在推送，请稍候')
    // ⭐ 「有没有原因可读」与上面那个后缀是**同一个判据**（复评 Minor-5 的时长靠它分流）。
    expect(pushResultHasReason(0, 2, ['原因甲'])).toBe(true)
    expect(pushResultHasReason(2, 0, ['原因甲'])).toBe(true)
    expect(pushResultHasReason(0, 2, [''])).toBe(false)
    expect(pushResultHasReason(0, 0, ['原因甲'])).toBe(false)
  })
})

/* ═══════════════════════════════════════════════════════════════════════════════════════
 * ⭐ 筛选接真 / 「更改状态」接真 / 「导出订单」CSV（业务方裁定）
 * 逐条依据见 `order-myorderlist.ts` 文件头「已知差异 12」。
 * ⚠️ 这一批用例的断言**一律钉字面值**（`E-0024`）：凡是「用生产常量去查生产常量」的写法都是
 * 自指的，改名时两边一起变、照样绿。
 * ═══════════════════════════════════════════════════════════════════════════════════════ */

/**
 * ⚠️ 必须落在**默认区间**（今天往前 6 个月）之内：挂载时那一次首屏渲染就带着日期筛。
 */
const ROW_B: MyOrderItem = {
  ...ROW,
  id: 8,
  order_no: '20260919080808080808',
  status: 'WAITING_GRAB',
  game_name: '鸣潮',
  project_text: '周本代打',
  is_urgent: true,
  created_at: '2026-09-19T08:00:00+08:00',
}

describe('本批新增的导出常量：**钉字面值**（改名 / 改词时这条会红）', () => {
  it('事件名与文案逐条等于证据件 / i18n 里的原值', () => {
    // ⚠️ 这些常量同时被**标记**（截进的 HTML）与**处理器**（`lay-event` 的比较）用；
    // 只钉「用常量查标记」是自指的（改名时两边一起变、照样绿）⇒ 必须钉字面值（E-0024）。
    expect(CHANGE_STATUS_EVENT).toBe('changeStaBtn')
    expect(EXPORT_EVENT).toBe('exportBtn')
    expect(CHANGE_STATUS_DIALOG_TITLE).toBe('更改订单状态')
    expect(SELECT_RECORDS_FIRST_TEXT).toBe('请选中要操作的记录')
    expect(CHANGE_STATUS_REQUIRES_IN_PROGRESS).toBe('只有代练中的单可以更改状态，当前状态：')
    expect(REPORT_ISSUE_SUCCESS_TEXT).toBe('已标记为问题单')
    expect(EXPORT_EMPTY_TEXT).toBe('没有可导出的订单')
    expect(CSV_COLUMNS.map((c) => c.title)).toEqual([
      '订单号',
      '游戏名称',
      '代练/陪玩内容',
      '订单状态',
      '接手时间',
      '加急',
    ])
    // ⭐ D-0301：「订单号」列的**值** = 展示单号（来源号非空取它、空回退内置号）。
    const noCol = CSV_COLUMNS[0]!.value
    expect(noCol(ROW)).toBe(ROW.order_no)
    expect(noCol({ ...ROW, source_order_no: 'SRC-TEST-1' })).toBe('SRC-TEST-1')
  })
})

describe('筛选：前端本地筛（业务方 2026-09-28 裁定「不动后端」）', () => {
  it('`applyFilters` 是纯函数：每条判据**各自能筛掉东西**（负向对照，防「条件被静默忽略」）', () => {
    // 只测「符合的留下」会漏掉恒真形态（E-0021 同族）⇒ 每条判据都配一条「不合就出局」。
    expect(applyFilters([ROW], EMPTY_FILTERS)).toEqual([ROW])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, gameName: '原神' })).toEqual([ROW])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, gameName: '鸣潮' })).toEqual([])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, statuses: ['IN_PROGRESS'] })).toEqual([ROW])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, statuses: ['ISSUE'] })).toEqual([])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, key1: '213215' })).toEqual([ROW])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, key1: '999' })).toEqual([])
    // ⭐ D-0301：单号搜索两个号都认——来源号能搜到 + 系统号仍能搜到。
    const srcRow: MyOrderItem = { ...ROW, source_order_no: 'SRC-TEST-1' }
    expect(applyFilters([srcRow], { ...EMPTY_FILTERS, key1: 'SRC-TEST' })).toEqual([srcRow])
    expect(applyFilters([srcRow], { ...EMPTY_FILTERS, key1: '213215' })).toEqual([srcRow])
    expect(applyFilters([srcRow], { ...EMPTY_FILTERS, key1: '999' })).toEqual([])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, key2: '深渊' })).toEqual([ROW])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, key2: '周本' })).toEqual([])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, urgent: true })).toEqual([])
    expect(
      applyFilters([{ ...ROW, is_urgent: true }], { ...EMPTY_FILTERS, urgent: true }),
    ).toHaveLength(1)
    // 日期是**闭区间**：`created_at` = 2026-09-20 ⇒ 两端相等要留下、越过任一端要出局。
    expect(
      applyFilters([ROW], { ...EMPTY_FILTERS, sDate: '2026-09-20', eDate: '2026-09-20' }),
    ).toEqual([ROW])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, sDate: '2026-09-21' })).toEqual([])
    expect(applyFilters([ROW], { ...EMPTY_FILTERS, eDate: '2026-09-19' })).toEqual([])
  })

  it('`statusesOfFilterValue`：中文 label → 枚举名、多选逗号串、认不出的档位**丢弃**', () => {
    expect(statusesOfFilterValue('代练中')).toEqual(['IN_PROGRESS'])
    expect(statusesOfFilterValue('代练中,问题单')).toEqual(['IN_PROGRESS', 'ISSUE'])
    // 证据件里那 7 档**逐档**都要认得出来（少一档 = 「这个状态选了等于没选」）。
    expect(statusesOfFilterValue('待抢单,代练中,待验收,已完成,问题单,已退单,已暂停')).toEqual([
      'WAITING_GRAB',
      'IN_PROGRESS',
      'PENDING_REVIEW',
      'COMPLETED',
      'ISSUE',
      'REFUNDED',
      'PAUSED',
    ])
    expect(statusesOfFilterValue('')).toEqual([])
    expect(statusesOfFilterValue('不存在的档位')).toEqual([])
  })

  it('`defaultDateRange`：今天往前 6 个月 ~ 今天（**按规则算**，不是证据件里那两个冻结值）', () => {
    // 传固定时刻 ⇒ 期望值是**字面值**（E-0024：不自指）。
    expect(defaultDateRange(new Date(2026, 8, 20))).toEqual(['2026-03-20', '2026-09-20'])
    // 跨年也要对（月份 −6 落回上一年）。
    expect(defaultDateRange(new Date(2026, 1, 15))).toEqual(['2025-08-15', '2026-02-15'])
  })

  it('挂载时：日期框填上默认区间、区服下拉按**我们的数据**重建（value = 我们的游戏名）', async () => {
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    const [from, to] = defaultDateRange()
    expect(root.querySelector<HTMLInputElement>('#sDate')!.value).toBe(from)
    expect(root.querySelector<HTMLInputElement>('#eDate')!.value).toBe(to)
    const options = Array.from(root.querySelectorAll<HTMLOptionElement>('#gameName option'))
    // 顺序按**拼音**排（`localeCompare(…, 'zh-Hans-CN')`：鸣 cháo < 原 yuán）——不是数据里的出现顺序。
    expect(options.map((o) => o.textContent)).toEqual(['全部游戏', '鸣潮', '原神'])
    // ⚠️ value 是**我们的 `game_name`**，⛔ 不是对标那些游戏 id（`21701` 这类我们没有，D-0093）。
    expect(options.map((o) => o.value)).toEqual(['', '鸣潮', '原神'])
  })

  it('首屏就带着默认日期区间筛（两行都在窗口内 ⇒ 两行都渲染）', async () => {
    await mountPage(defaultImpl([ROW, ROW_B]))
    const opts = LAYUI.render.mock.calls[0]?.[0] as { data?: MyOrderItem[] }
    expect(opts.data?.map((d) => d.order_no)).toEqual([ROW.order_no, ROW_B.order_no])
  })

  it('点「搜索」：按表单当前值重筛，**零业务请求**（数据早在手上）', async () => {
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    root.querySelector<HTMLInputElement>('#key2')!.value = '周本'
    const before = mockedRequest.mock.calls.length
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    const opts = LAYUI.reload.mock.calls[LAYUI.reload.mock.calls.length - 1]?.[1] as {
      data?: MyOrderItem[]
    }
    expect(opts.data?.map((d) => d.order_no)).toEqual([ROW_B.order_no])
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('勾「加急单」：即时重筛 + 照对标弹 `layer.tips`（文案取元素的 `lay-tips`）', async () => {
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    root.querySelector<HTMLInputElement>('input[name="isJiajiCheck"]')!.checked = true
    LAYUI.formHandlers.get('checkbox(viewIsJiaJi)')?.()
    const opts = LAYUI.reload.mock.calls[LAYUI.reload.mock.calls.length - 1]?.[1] as {
      data?: MyOrderItem[]
    }
    expect(opts.data?.map((d) => d.order_no)).toEqual([ROW_B.order_no])
    expect(LAYUI.tips).toHaveBeenCalledWith('勾选后将筛选出加急的订单', '#isJiajiCheck', {
      tips: 1,
      time: 2000,
    })
  })

  it('切区服：即时重筛（对标 `select(selectGame)` 也是直接刷）', async () => {
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    root.querySelector<HTMLSelectElement>('#gameName')!.value = '鸣潮'
    LAYUI.formHandlers.get('select(selectGame)')?.()
    const opts = LAYUI.reload.mock.calls[LAYUI.reload.mock.calls.length - 1]?.[1] as {
      data?: MyOrderItem[]
    }
    expect(opts.data?.map((d) => d.order_no)).toEqual([ROW_B.order_no])
  })

  it('「刷新列表」重新取数后**保留**当前筛选（刷一次就清掉会被当成「筛选没生效」）', async () => {
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    root.querySelector<HTMLInputElement>('#key2')!.value = '周本'
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    LAYUI.reload.mockClear()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'refresh' })
    await vi.waitFor(() => expect(LAYUI.reload).toHaveBeenCalled())
    const opts = LAYUI.reload.mock.calls[LAYUI.reload.mock.calls.length - 1]?.[1] as {
      data?: MyOrderItem[]
    }
    expect(opts.data?.map((d) => d.order_no)).toEqual([ROW_B.order_no])
  })

  it('证据件里那 8 个筛选控件真的没了（⛔ 半新半旧：标记删了、代码还在找它）', () => {
    for (const needle of [
      'id="gameArea"',
      'id="gameServer"',
      'id="gameJob"',
      'id="gameGroup"',
      'id="bussType"',
      'id="key4"',
      'id="gameAcc"',
      'name="viewIsNewOrder"',
    ]) {
      expect(EVIDENCE_MARKUP).not.toContain(needle)
    }
  })

  it('「搜索1」的 placeholder =「订单号/来源单号」（⭐ D-0301；仍搜不了角色名/手机号）', () => {
    // 只认**那个输入框**那一段（不拿整份文件）：留痕注释里也会提到旧 placeholder 原文。
    const key1 = /<input[^>]*id="key1"[^>]*>/.exec(EVIDENCE_MARKUP)?.[0] ?? ''
    expect(key1).not.toBe('')
    expect(key1).toContain('placeholder="订单号/来源单号"')
    expect(key1).not.toContain('角色名')
    expect(key1).not.toContain('手机号')
  })
})

describe('「更改状态」：受限两档（业务方 2026-09-28 裁定）', () => {
  /** 走真实入口（工具栏事件名逐字取自证据件）打开弹层，返回 `layer.open` 的入参。 */
  function openDialog(root: HTMLElement, rows: MyOrderItem[]): Record<string, unknown> {
    LAYUI.checkStatus.mockReturnValue({ data: rows })
    LAYUI.open.mockClear()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'changeStaBtn' })
    expect(LAYUI.open, '点了「更改状态」没有开层').toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('层参数：五处逐项取本页对标 `myorderlist.js:1358-1365`，**只有 `type` 是 1**（有意偏离）', async () => {
    const root = await mountPage()
    const opts = openDialog(root, [ROW])
    expect(opts['title']).toBe('更改订单状态')
    expect(opts['shade']).toEqual([0.3, '#333'])
    expect(opts['shadeClose']).toBe(true)
    expect(opts['skin']).toBe('layui-layer-lan')
    expect(opts['anim']).toBe(5)
    expect(opts['area']).toEqual(['80%', '240px'])
    // ⛔ 对标是 `type: 2`（装 `SetOrderSta` iframe、13 个状态随便选）。我们是 1（自己两个 radio）
    // ——见文件头 12-(c)，这是**如实登记的偏离**，不是抄错。
    expect(opts['type']).toBe(1)
    expect(opts['btn']).toEqual(['确定', '取消'])
  })

  it('弹层内容**恰好两档**（待验收 / 问题单），默认选第一档（⛔ 没有第三档）', async () => {
    const root = await mountPage()
    const box = openDialog(root, [ROW])['content'] as HTMLElement
    const radios = Array.from(box.querySelectorAll<HTMLInputElement>('input[name="target"]'))
    expect(radios.map((r) => r.value)).toEqual(['PENDING_REVIEW', 'ISSUE'])
    expect(radios.map((r) => r.getAttribute('title'))).toEqual(['待验收', '问题单'])
    expect(radios[0]!.checked).toBe(true)
  })

  it('未勾选任何行 ⇒ 对标那句「请选中要操作的记录」+ **不开层**', async () => {
    await mountPage()
    LAYUI.open.mockClear()
    LAYUI.checkStatus.mockReturnValue({ data: [] })
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'changeStaBtn' })
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('请选中要操作的记录', { time: 1500 })
  })

  it('勾选的行里有**不是代练中**的 ⇒ 固定文案 + 不开层 + 零请求（前端只是省一趟无谓请求）', async () => {
    await mountPage()
    LAYUI.open.mockClear()
    LAYUI.checkStatus.mockReturnValue({ data: [{ ...ROW, status: 'PENDING_REVIEW' }] })
    const before = mockedRequest.mock.calls.length
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'changeStaBtn' })
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).toHaveBeenCalledWith('只有代练中的单可以更改状态，当前状态：待验收', {
      time: 6000,
    })
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('选「待验收」+ 确定 ⇒ POST `/submit/`（复用既有端点）+ 自拟成功提示', async () => {
    const root = await mountPage()
    const opts = openDialog(root, [ROW])
    const box = opts['content'] as HTMLElement
    box.querySelector<HTMLInputElement>('input[value="PENDING_REVIEW"]')!.checked = true
    const before = mockedRequest.mock.calls.length
    ;(opts['yes'] as (i: number) => void)(0)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('提交成功，已送交验收', { time: 1500 })
    })
    const urls = mockedRequest.mock.calls.slice(before).map((c) => (c[0] as ReqConfig).url)
    expect(urls).toEqual([
      `/api/business/orders/${ROW.order_no}/submit/`,
      '/api/business/my-orders/',
    ])
  })

  it('选「问题单」时必须填原因，提交时去掉首尾空白', async () => {
    const root = await mountPage()
    const opts = openDialog(root, [ROW])
    const box = opts['content'] as HTMLElement
    box.querySelector<HTMLInputElement>('input[value="ISSUE"]')!.click()
    const reason = box.querySelector<HTMLInputElement>('input[aria-label="说明原因"]')!
    expect(reason.closest('.layui-form-item')?.getAttribute('style')).not.toContain('display: none')
    const before = mockedRequest.mock.calls.length
    const closeCount = LAYUI.close.mock.calls.length
    reason.value = ' 　'
    ;(opts['yes'] as (i: number) => void)(0)
    expect(mockedRequest.mock.calls).toHaveLength(before)
    expect(LAYUI.close).toHaveBeenCalledTimes(closeCount)
    expect(LAYUI.msg).toHaveBeenCalledWith('请输入问题原因', { time: 6000 })

    reason.value = ' 账号掉线 '
    ;(opts['yes'] as (i: number) => void)(0)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已标记为问题单', { time: 1500 })
    })
    const configs = mockedRequest.mock.calls.slice(before).map((c) => c[0] as ReqConfig & { data?: unknown })
    expect(configs.map((c) => `${c.method} ${c.url}`)).toEqual([
      `post /api/business/orders/${ROW.order_no}/report-issue/`,
      'get /api/business/my-orders/',
    ])
    expect(configs[0]!.data).toEqual({ reason: '账号掉线' })
  })

  it('批量：勾了两行、选「待验收」⇒ **逐单**各发一条（顺序与勾选顺序一致）', async () => {
    // ⚠️ `ROW_B` 的状态是「待抢单」（它专门用来测状态筛选）⇒ 这里必须换成「代练中」，
    //    否则会被 `requestChangeStatus` 的来源态判据挡在**开层之前**（测试会以「没开层」失败）。
    const second: MyOrderItem = { ...ROW_B, status: 'IN_PROGRESS' }
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    const opts = openDialog(root, [ROW, second])
    const before = mockedRequest.mock.calls.length
    ;(opts['yes'] as (i: number) => void)(0)
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalled())
    const urls = mockedRequest.mock.calls.slice(before).map((c) => (c[0] as ReqConfig).url)
    expect(urls).toEqual([
      `/api/business/orders/${ROW.order_no}/submit/`,
      `/api/business/orders/${second.order_no}/submit/`,
      '/api/business/my-orders/',
    ])
  })

  it('部分失败：成功的笔数 + **后端原文**第一条一起报出来，且照旧刷新（改过的东西要看见）', async () => {
    const second: MyOrderItem = { ...ROW_B, status: 'IN_PROGRESS' }
    const root = await mountPage(async (config) => {
      if (config.url === '/api/auth/me') return ME_FULL
      if (config.method === 'post' && String(config.url).includes(second.order_no)) {
        throw new BizError(1002, '只有代练中的单可以提交，当前状态：待验收')
      }
      if (config.method === 'post') return { order_no: ROW.order_no, status: 'PENDING_REVIEW' }
      return { items: [ROW, ROW_B], total: 2, page: 1, page_size: 200 }
    })
    const opts = openDialog(root, [ROW, second])
    LAYUI.reload.mockClear()
    ;(opts['yes'] as (i: number) => void)(0)
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '已更改 1 笔；只有代练中的单可以提交，当前状态：待验收',
        { time: 6000 },
      )
    })
    expect(LAYUI.reload).toHaveBeenCalled()
  })

  it('行内状态徽标走**同一个**处理器：代练中的行点它也能开层', async () => {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[9]!.templet(ROW)
    root.appendChild(wrap)
    LAYUI.open.mockClear()
    wrap.querySelector<HTMLElement>('a.setdlsta')!.click()
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect((LAYUI.open.mock.calls[0]![0] as Record<string, unknown>)['title']).toBe('更改订单状态')
  })

  it('行内徽标：状态**不是代练中**时点它 ⇒ 提示而不是开层（`data-status` 是承重的）', async () => {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildCols()[9]!.templet({ ...ROW, status: 'COMPLETED' })
    root.appendChild(wrap)
    LAYUI.open.mockClear()
    wrap.querySelector<HTMLElement>('a.setdlsta')!.click()
    expect(LAYUI.open).not.toHaveBeenCalled()
    // ⚠️ 期望值钉的是**那个状态的中文**：把 templet 里的 `data-status` 去掉，这里会变成
    // 空串 ⇒ 「当前状态：」后面什么都没有，本条当场红（判据不是恒真的）。
    expect(LAYUI.msg).toHaveBeenCalledWith('只有代练中的单可以更改状态，当前状态：已完成', {
      time: 6000,
    })
  })

  it('权限门：没有 `order.submit_work` ⇒ 工具栏模板里**搜不到**那颗按钮', async () => {
    const impl: ReqImpl = async (config) => {
      if (config.url === '/api/auth/me') return { user: { menu_permissions: [] } }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    }
    const root = await mountPage(impl)
    const template = root.querySelector<HTMLScriptElement>('#toolbarDemo')?.textContent ?? ''
    expect(template).not.toContain('changeStaBtn')
    // 负向对照：同一次挂载里，不受这个门管的按钮**还在**（否则上面的「搜不到」也可能只是
    // 模板整个没渲染出来 —— 那就成了恒真的判据）。
    expect(template).toContain('exportBtn')
    expect(template).toContain('sendToMySystem')
  })

  it('权限门：有 `order.submit_work`（默认夹具）⇒ 那颗按钮在', async () => {
    const root = await mountPage()
    const template = root.querySelector<HTMLScriptElement>('#toolbarDemo')?.textContent ?? ''
    expect(template).toContain('lay-event="changeStaBtn"')
  })
})

describe('「导出订单」：前端生成 CSV（业务方 2026-09-28 选定）', () => {
  /** 打桩落盘出口：`URL.createObjectURL`（jsdom 没有）+ 拦掉 `<a>` 的点击（否则 jsdom 会试导航）。 */
  function stubDownload(): { blobs: Blob[] } {
    const blobs: Blob[] = []
    const url = URL as unknown as {
      createObjectURL: (b: Blob) => string
      revokeObjectURL: (u: string) => void
    }
    url.createObjectURL = (b: Blob) => {
      blobs.push(b)
      return 'blob:fake'
    }
    url.revokeObjectURL = () => {}
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    return { blobs }
  }

  it('`buildCsv`：表头逐字取列定义、CRLF 换行、每个单元格都加引号', () => {
    const lines = buildCsv([ROW]).split('\r\n')
    expect(lines).toHaveLength(2)
    expect(lines[0]).toBe('"订单号","游戏名称","代练/陪玩内容","订单状态","接手时间","加急"')
    expect(lines[1]).toBe(
      '"20260920213215233029","原神","深渊满星 12-3","代练中","2026-09-20 21:32","否"',
    )
    // 加急那一列取的是**徽标承载的可见事实**（`is_urgent`），不是表格列。
    expect(buildCsv([ROW_B]).split('\r\n')[1]).toContain('"是"')
  })

  it('`csvCell`：内层引号翻倍 + **公式注入前缀转义**（`=` / `+` / `-` / `@` / Tab / CR）', () => {
    expect(csvCell('a"b')).toBe('"a""b"')
    expect(csvCell('=1+1')).toBe('"\'=1+1"')
    expect(csvCell('+1')).toBe('"\'+1"')
    expect(csvCell('-1')).toBe('"\'-1"')
    expect(csvCell('@x')).toBe('"\'@x"')
    expect(csvCell('\tx')).toBe('"\'\tx"')
    // 正常文本不动（⛔ 别把前缀转义写成「一律加撇」）。
    expect(csvCell('正常文本')).toBe('"正常文本"')
  })

  it('`csvFilename`：`我的订单_YYYYMMDDHHMMSS.csv`（本地时间、补零）', () => {
    expect(csvFilename(new Date(2026, 8, 28, 9, 5, 3))).toBe('我的订单_20260928090503.csv')
  })

  it('点「导出订单」（没勾行）⇒ 导**当前筛出来的**行 + 带 BOM + 提示笔数', async () => {
    const root = await mountPage(defaultImpl([ROW, ROW_B]))
    // 先筛成 1 行 ⇒ 证明导的是「筛出来的」而不是「全部」。
    root.querySelector<HTMLInputElement>('#key2')!.value = '周本'
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    const { blobs } = stubDownload()
    LAYUI.msg.mockClear()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'exportBtn' })
    expect(LAYUI.msg).toHaveBeenCalledWith('已导出 1 笔订单', { time: 1500 })
    expect(blobs).toHaveLength(1)
    // ⚠️ **不能用 `blob.text()` 判 BOM**：那个 API 走 UTF-8 decode，会**把 BOM 吃掉**
    //（实测：`new Blob(['\uFEFFx']).text()` 得到 `'x'`）⇒ 必须看原始字节。
    const bytes = new Uint8Array(await blobs[0]!.arrayBuffer())
    expect(Array.from(bytes.slice(0, 3))).toEqual([0xef, 0xbb, 0xbf])
    const text = await blobs[0]!.text()
    expect(text).toContain(`"${ROW_B.order_no}"`)
    expect(text).not.toContain(ROW.order_no)
  })

  it('勾了行 ⇒ 只导勾选的那几行（**不看**当前筛选）', async () => {
    await mountPage(defaultImpl([ROW, ROW_B]))
    LAYUI.checkStatus.mockReturnValue({ data: [ROW] })
    const { blobs } = stubDownload()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'exportBtn' })
    const text = await blobs[0]!.text()
    expect(text).toContain(ROW.order_no)
    expect(text).not.toContain(ROW_B.order_no)
  })

  it('一行都没有 ⇒ 一句提示、**不产生空文件**', async () => {
    await mountPage(defaultImpl([]))
    const { blobs } = stubDownload()
    LAYUI.msg.mockClear()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'exportBtn' })
    expect(LAYUI.msg).toHaveBeenCalledWith('没有可导出的订单', { time: 1500 })
    expect(blobs).toHaveLength(0)
  })
})
