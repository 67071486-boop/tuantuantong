// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * ⭐ D-0365：日期默认区间改为运行期计算（页面挂载时写 `#sDate`/`#eDate`）
 * ⇒ 替身成**固定值**，首屏 GET 的日期参数才能按**字面值**断言（E-0024：不从实现派生期望值）。
 * ⛔ 评审 F-1 改准：本页偏移是 **6 个自然月**（证据件 `Emp__myemp.html` L119/L123 =
 * 快照日 2026-09-20 的 `2026-03-20` / `2026-09-20`），不是 emplist 的 3 ⇒
 * 替身值 = 快照日式 Today(2026-11-02) − 6 个月 = `2026-05-02`。
 */
vi.mock('@/layui-island/dateRange', () => ({
  defaultDateRange: () => ({ sDate: '2026-05-02 00:00:00', eDate: '2026-11-02 23:59:59' }),
}))

vi.mock('@/api/http', () => ({
  request: vi.fn(),
  // ⭐ D-0365：导出走 `downloadMyOrderExport` → `http.get`（Blob 形状）⇒ 替身要给 `http`。
  http: { get: vi.fn() },
}))

/**
 * layui 的**可控替身**（与 `order-myorderlist.spec.ts` 同一手法）。`bootstrapIsland` 在 jsdom 里
 * 加载不了真的 layui，所以换成替身之后 `layer.msg` / `table.render` / `table.reload` 的
 * **入参**才能被断言、layui 自己的事件（toolbar / submit / select）才能手动触发。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  // ⭐ T5：「点单号 ⇒ 原地开层」的落地出口（`layer.open` 的入参在这里被断言）。
  const open = vi.fn()
  const reload = vi.fn()
  const render = vi.fn()
  /** `table.getOptions(id)`：生产代码用它读用户选的「条/页」实时值（E-0037 家族）。 */
  const getOptions = vi.fn()
  /** ⭐ D-0365：勾选行读取出口（`table.checkStatus(id)`），申请结算的 id 来源。 */
  const checkStatus = vi.fn()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  return {
    msg,
    open,
    reload,
    render,
    getOptions,
    checkStatus,
    tableHandlers,
    formHandlers,
    form: {
      render: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: {
      render,
      reload,
      getOptions,
      checkStatus,
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: { open, msg },
    element: { init: vi.fn() },
  }
})

/**
 * formSelects 的替身。**生产代码读订单状态走的是 `getFormSelects()`**
 * （页面模块从 `@/layui/vendor` import 它），而 jsdom 里 `window.layui` 不存在 ⇒ 真实现永远回
 * `undefined`、测试会**静默**只跑到原生 `<select>` 兜底那支（评审 2026-09-21 实测：两条变异都杀不掉）。
 * 所以这里把 `getFormSelects` 换成可控替身，让**真路径**被断言。
 *
 * `current` 可在单个用例里置 `undefined`，用来钉「拿不到实例时退回原生 select」那条兜底。
 */
const FS = vi.hoisted(() => {
  const value = vi.fn()
  const render = vi.fn()
  return {
    value,
    render,
    current: undefined as unknown,
    instance: { value, render },
  }
})

vi.mock('@/layui/vendor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/vendor')>()
  return { ...actual, getFormSelects: () => FS.current }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

import { request, http } from '@/api/http'
import { HttpError } from '@/api/errors'
// `MyOrderItem` 归 `api/myOrders` 所有，页面模块只是 import 它、没有转发 ⇒ 从源头取
// （`pnpm build` 会连 vitest 工程一起类型检查，这条只有它能抓到）。
import type { MyOrderItem } from '@/api/myOrders'
import {
  applyFilters,
  buildRuntimeCols,
  COLS,
  DATE_TYPE_MAP,
  EMPTY_SELECTION_TEXT,
  PAGE_LIMIT,
  PAGE_LIMITS,
  STATUS_BADGE_COLORS,
  STATUS_BADGE_FALLBACK,
  TABLE_ELEM,
  TOOLBAR_ELEM,
  TOOLBAR_NOT_OPEN_EVENTS,
  TOTAL_TEXT,
  UNPARSABLE_DATE_TEXT,
  type MyEmpFilters,
} from '../emp-myemp'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/emp-myemp.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(path.resolve(HERE, '../../../../public/replica/emp-myemp.html'), 'utf8')

/**
 * 工具栏模板的**原文内容**（`#toolbarDemo`）。真 layui 会把它渲染进 `.layui-table-tool`；
 * jsdom 里没有真 layui，所以测试自己按同一层次注入——这样「尚未开放」的文案是从
 * **证据件**取的，不是测试里另抄一份中文。
 */
const TOOLBAR_HTML = /<script type="text\/html" id="toolbarDemo">([\s\S]*?)<\/script>/.exec(
  EVIDENCE,
)?.[1]

const mockedRequest = vi.mocked(request)
const mockedHttpGet = vi.mocked(http.get)

/**
 * ⭐ D-0365：首屏 GET 的**日期参数**（挂载时把替身的默认区间写进两个日期框 ⇒
 * 首屏与刷新的 GET 都带 `date_type=claimed` + 两端；字面值见上面的 dateRange 替身）。
 * 既有用例的 GET 形状断言全部用它，避免三处各抄一份。
 */
const DEFAULT_DATE_PARAMS = {
  date_type: 'claimed',
  date_from: '2026-05-02',
  date_to: '2026-11-02',
}

/**
 * 页面拿到的 `ctx.onNavigate`。⭐ **T5 起它是「负向对照」**：本页唯一的「进订单详情」入口
 * （单号蓝链）已改成**原地开层**，所以这条出口**一次都不该被调用**（见 T5 那组用例）。
 */
const navigate = vi.fn()

/** 一行真数据（字段与后端 `MyOrderSerializer` 同形，含本轮新补的字段）。 */
const ROW: MyOrderItem = {
  id: 7,
  order_no: '20260920213215233029',
  source_order_no: 'TB-合成-我的收入-来源甲',
  status: 'IN_PROGRESS',
  game_name: '原神',
  project_text: '深渊满星 12-3',
  is_urgent: false,
  temp_amount: '12.34',
  bonus_amount: '10.00',
  review_status: '已通过',
  created_at: '2026-09-20T21:32:15+08:00',
  username: '打手小李',
  // ⭐ 这两格来自 `MyOrderSerializer`（「指派给我的订单」第 12/13 列）；
  // 本页不渲染它们，但类型是必填的 ⇒ 夹具按后端形状补上（值写真实形态，不是占位空串）。
  taker_name: '打手小李',
  taker_price: '12.34',
  emp_type: '打单工资',
  settle_status: '未结账',
  // ⭐ D-0365：`settle_requested_at`（2026-10-23 起就在载荷里；本批接「是否已申请结算」列）。
  settle_requested_at: null,
}

/** 有提前结算申请的一行（第 18 列「已申请」的那一档）。 */
const ROW_APPLIED: MyOrderItem = {
  ...ROW,
  id: 10,
  order_no: '20260920213215233040',
  settle_requested_at: '2026-11-01T10:00:00+08:00',
}

/** 第二行：用来证明筛选真的收窄（不同的结算状态 / 游戏 / 单号 / 项目）。 */
const ROW_SETTLED: MyOrderItem = {
  ...ROW,
  id: 8,
  order_no: '20260920213215233030',
  status: 'COMPLETED',
  game_name: '崩坏：星穹铁道',
  project_text: '忘却之庭满星',
  settle_status: '已结账',
}

/** 第三行：`WAITING_GRAB`。 */
const ROW_WAITING: MyOrderItem = {
  ...ROW,
  id: 9,
  order_no: '20260920213215233031',
  status: 'WAITING_GRAB',
  project_text: '深境螺旋',
}

/**
 * 证据件订单状态下拉的**7 个状态档**：`[option 文案, 后端枚举名]`。
 * ⚠️ 第一档是「待抢单」——证据件已按业务方 2026-09-21 的裁定改过（见文件头登记第 13 条），
 * 不再有「待上号」这个别名。文案是**字面值**，枚举名也是字面值（E-0024）。
 */
const STATUS_CASES = [
  ['待抢单', 'WAITING_GRAB'],
  ['代练中', 'IN_PROGRESS'],
  ['待验收', 'PENDING_REVIEW'],
  ['已完成', 'COMPLETED'],
  ['问题单', 'ISSUE'],
  ['已退单', 'REFUNDED'],
  ['已暂停', 'PAUSED'],
] as const

/** 7 行，每行一个状态——用来逐档验「选项 → 枚举 → 收窄后的行」。 */
const STATUS_ROWS: MyOrderItem[] = STATUS_CASES.map(([, status], i) => ({
  ...ROW,
  id: 100 + i,
  order_no: `202609202132152330${String(i + 10)}`,
  status,
}))

/**
 * 「订单状态」下拉**整段**（`select[name=orderSta]`）的**原文**。守卫测试只认这一段里的
 * 可见 `<option>`——不拿「整份证据件里没有『待上号』」当判据：证据件**头部**的偏差登记里
 * 就写着「待上号」三个字（说明我们改了什么），别处也可能出现同词。
 */
const STA_SELECT = /<select name="orderSta"[\s\S]*?<\/select>/.exec(EVIDENCE)?.[0] ?? ''

/** 空筛选（5 个条件都不设）——`applyFilters` 的"全放行"输入。 */
const NO_FILTER: MyEmpFilters = {
  game: '',
  settleStatus: '',
  orderStatuses: [],
  key1: '',
  key2: '',
}

function injectToolbar(root: HTMLElement): void {
  const wrap = document.createElement('div')
  wrap.className = 'layui-table-tool'
  wrap.innerHTML = TOOLBAR_HTML ?? ''
  root.appendChild(wrap)
}

type ReqImpl = (config: { method?: string; url?: string }) => Promise<unknown>

/** 默认后端：列表回 `rows`。 */
function defaultImpl(rows: MyOrderItem[]): ReqImpl {
  return async () => ({ items: rows, total: rows.length, page: 1, page_size: 200 })
}

/** 挂整页（取数走**真的** `api/myOrders.ts` → 打桩的 `request`）。 */
async function mountPage(rowsOrImpl: MyOrderItem[] | ReqImpl = [ROW]): Promise<HTMLElement> {
  const impl = typeof rowsOrImpl === 'function' ? rowsOrImpl : defaultImpl(rowsOrImpl)
  mockedRequest.mockImplementation(impl as never)
  const root = document.createElement('div')
  document.body.appendChild(root)
  const { mountMyEmpPage } = await import('../emp-myemp')
  await mountMyEmpPage(root, { onNavigate: navigate })
  injectToolbar(root)
  return root
}

beforeEach(() => {
  mockedRequest.mockReset()
  mockedHttpGet.mockReset()
  navigate.mockClear()
  LAYUI.msg.mockClear()
  LAYUI.reload.mockClear()
  LAYUI.render.mockClear()
  LAYUI.getOptions.mockReset()
  // ⭐ D-0365：默认没人勾选（申请结算的空选分支走提示）。
  LAYUI.checkStatus.mockReset()
  LAYUI.checkStatus.mockReturnValue({ data: [] })
  // 默认：layui 还没报出实时配置（⇒ 生产代码回落到 `PAGE_LIMIT`）。要断「保留用户条/页」的
  // 用例自己给返回值。
  LAYUI.getOptions.mockReturnValue(undefined)
  // 默认把 formSelects 替身挂上（`getFormSelects()` 回它），且当前无选中档位；
  // 要钉原生 `<select>` 兜底的用例把 `FS.current = undefined`。
  FS.value.mockReset()
  FS.value.mockReturnValue([])
  FS.render.mockClear()
  FS.current = FS.instance
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('emp-myemp.html')) {
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
  document.body.innerHTML = ''
})

/**
 * 【我的收入】`/Emp/myemp` 的列声明与统计文案骨架。
 *
 * 这四条断言盯的都是「漏了就**静默**出错、探针未必报」的项：
 * 特殊列 `type`、`hide` 的运行期取值、重复 `field`、`page.limits` 的档数。
 * 出处一律是对标产物 `.tools/recon/suqing1/assets/JS2019/Emp/myemp.js`。
 *
 * ⚠️ 本页 2026-09-21 从 `artifactPage` 改手写 mount 后，这些声明值**一个都没动**
 * （运行期列由 `buildRuntimeCols()` 从 `COLS` 派生，只按下标补 templet）——这一组是防漂移的钉子。
 */
describe('我的收入（/Emp/myemp）列声明', () => {
  it('19 列；勾选列带 type:checkbox + fixed:left + width:30', () => {
    // 出处：`myemp.js` L596 `{ type:'checkbox', fixed:'left', width:30 }`。
    expect(TABLE_ELEM).toBe('#tableList')
    expect(TOOLBAR_ELEM).toBe('#toolbarDemo')
    expect(COLS.length).toBe(19)
    expect(COLS[0]?.type).toBe('checkbox')
    expect(COLS[0]?.fixed).toBe('left')
    expect(COLS[0]?.width).toBe(30)
  })

  it('表头文案序列与 myemp.js L596-737 一致（含字典里被覆盖的 orderId）', () => {
    // ⚠️ i18n 字典里 `orderId` 定义了两次（L310 '关联订单号' → L314 '系统内置单号'，后者生效），
    // 两列的 title 都是「系统内置单号」。
    expect(COLS.map((c) => c.title)).toEqual([
      undefined,
      '用户名称',
      '预计收入<i class="layui-icon layui-bangzhu" lay-tips="代表预计会获得的订单收入"></i>',
      '实际收入<i class="layui-icon layui-bangzhu" lay-tips="表示最终您获得的订单收入，只有当您亲自提交了完单成后该金额才会生效"></i>',
      '扣点',
      '收入类型',
      '系统内置单号',
      '系统内置单号',
      '角色名',
      '游戏名称',
      '代练项目',
      '订单状态',
      '记录时间',
      '提交时间',
      '记录人',
      '结算状态',
      '清算时间',
      '结账方式',
      '是否已申请结算<i class="layui-icon layui-bangzhu" title="你可以主动申请结算哪些订单。操作步骤：1、勾选要清算的订单；2、点击列表头部的申请结算按钮"></i>',
    ])
    // 第 7、8 列**同 field**（`orderId`，对标原文如此，不要"修好"）
    expect(COLS[6]?.field).toBe('orderId')
    expect(COLS[7]?.field).toBe('orderId')
  })

  it('hide 两列：扣点（hide 是运行期取值 `!showDeductFee`）+ 第二个系统内置单号', () => {
    // `deductFeeType = 0`（对标 HTML L187-189）⇒ `showDeductFee = false` ⇒ `hide: !false = true`。
    expect(COLS[4]?.field).toBe('deductFee')
    expect(COLS[4]?.hide).toBe(true)
    expect(COLS[7]?.hide).toBe(true)
    // 其余列**没有** hide（少写一个 hide 就会多出一列，探针会报）
    const hidden = COLS.filter((c) => c.hide === true).map((c) => c.field)
    expect(hidden).toEqual(['deductFee', 'orderId'])
  })

  it('`dlProject` 没有 width（自动列，补 width 会撑破总宽）', () => {
    const dl = COLS.find((c) => c.field === 'dlProject')
    expect(dl?.width).toBeUndefined()
    expect(dl?.minWidth).toBeUndefined()
  })

  it('分页：limit 20、limits 6 档（对标 `page:true`，本批少数显式开分页的页）', () => {
    // 出处：`myemp.js` L739-741 `page:true, limit:20, limits:[10,20,30,40,50,100]`。
    expect(PAGE_LIMIT).toBe(20)
    expect(PAGE_LIMITS).toEqual([10, 20, 30, 40, 50, 100])
  })

  it('#total 文案骨架 = myemp.js L744 的 dataTotal1..8 拼接（0 行 ⇒ 7 个数字槽都是 0）', () => {
    expect(TOTAL_TEXT).toBe(
      '您接手过的订单，都会在此产生账单，本次筛选条件下共计0条账单，总计(不含已退单)：预计收入0元，' +
        '实际收入0元，已结账0笔（0元）,还未结账0笔（预计有0元），默认显示最近6个月数据，' +
        '若需扩大时间范围，可手动选择起始日期',
    )
    // 逗号前无空格、「共有」处无空格、`(不含已退单)` 是半角括号、`（` 是全角——逐字对齐的坑点
    expect(TOTAL_TEXT).toContain('共计0条账单，总计(不含已退单)：预计收入0元')
  })

  it('运行期列从 COLS 派生：顺序/宽度/隐藏/对齐不动，只补 templet（D-0365：18 列有 templet）', () => {
    const runtime = buildRuntimeCols()
    expect(runtime.length).toBe(19)
    // 勾选列是 type 特殊列 ⇒ **不补** templet（layui 自己渲染勾选框）
    expect(runtime[0]?.type).toBe('checkbox')
    expect(runtime[0]?.templet).toBeUndefined()
    // 真值列有 templet；缺字段列也有（渲染成空串），但声明值逐项照抄
    // ⛔ D-0365 改准：18（是否已申请结算）从此有 templet。
    for (const i of [1, 2, 3, 5, 6, 7, 9, 10, 11, 12, 15, 18]) {
      expect(typeof runtime[i]?.templet).toBe('function')
    }
    expect(runtime[4]?.hide).toBe(true)
    expect(runtime[7]?.hide).toBe(true)
    expect(runtime[10]?.width).toBeUndefined()
    expect(runtime[18]?.width).toBe(110)
  })
})

describe('列表取数：真的走 GET /api/business/my-orders/', () => {
  it('挂载即发 GET（方法 + URL 字面值 + ⭐D-0365 默认日期窗口），并把返回行交给 layui 渲染', async () => {
    await mountPage()
    expect(mockedRequest).toHaveBeenCalledWith({
      method: 'get',
      url: '/api/business/my-orders/',
      params: { page: 1, page_size: 200, ...DEFAULT_DATE_PARAMS },
    })
    const opts = LAYUI.render.mock.calls[0]?.[0] as { data?: MyOrderItem[] }
    expect(opts.data).toEqual([ROW])
  })

  it('无节点 `my_orders.send_to_my_system`（403）→ 后端 msg **原样**显示，且表被清空（不是静默空表）', async () => {
    await mountPage(async () => {
      // ⚠️ 闸门是**节点**（不是 `Role.TAKER`）：本用例只钉
      // 「前端把后端 msg 原样弹出来」，不钉具体文案。
      throw new HttpError(403, '你没有查看我的订单的权限')
    })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('你没有查看我的订单的权限', { time: 6000 })
    })
    const opts = LAYUI.render.mock.calls[0]?.[0] as { data?: MyOrderItem[] }
    expect(opts.data).toEqual([])
  })
})

describe('单元格渲染：只填后端真回了的列', () => {
  const cols = buildRuntimeCols()
  const cell = (i: number, d: MyOrderItem = ROW): string => cols[i]?.templet?.(d) ?? ''

  it('真值列渲染载荷里的**字面值**（用户名 / 两个金额 / 收入类型 / 单号 / 游戏 / 项目 / 记录时间 / 结算状态）', () => {
    expect(cell(1)).toBe('<span>打手小李</span>')
    expect(cell(2)).toContain('>12.34</span>')
    expect(cell(3)).toContain('>10.00</span>')
    expect(cell(5)).toBe('<span style="color:#E03636">打单工资</span>')
    expect(cell(6)).toContain('>20260920213215233029</a>')
    // 隐藏的第二个「系统内置单号」也是真值
    expect(cell(7)).toBe('20260920213215233029')
    expect(cell(9)).toBe('原神')
    expect(cell(10)).toBe('<span>深渊满星 12-3</span>')
    expect(cell(12)).toBe('<span>2026-09-20 21:32</span>')
    expect(cell(15)).toContain('>未结账</a>')
  })

  it('订单号列（下标 6）是对标的蓝链，`data-id` / 文本填 `order_no`（不是绩效主键）', () => {
    const html = cell(6)
    expect(html).toContain('class="orderdetails"')
    expect(html).toContain('href="javascript:void(0);"')
    expect(html).toContain('color:#0094ff')
    expect(html).toContain('data-id="20260920213215233029"')
    expect(html).toContain('data-order="20260920213215233029"')
    expect(html).toContain('title="点击查看订单详情"')
    // `ROW.id`（= 7）是**绩效行主键**，不该出现在订单 id 位上
    expect(html).not.toContain('data-id="7"')
  })

  it('5 个缺字段列 + 隐藏的扣点列渲染成**空**，不造值、不出现编造的中文（D-0365 改准：18 列已接）', () => {
    // ⛔（D-0365）下标 18（是否已申请结算）**已接上**（`settle_requested_at`，见下一条）；
    //    真正留空的是下标 4 / 8 / 13 / 14 / 16 / 17。
    for (const i of [4, 8, 13, 14, 16, 17]) {
      expect(cell(i)).toBe('')
    }
    // 对标这几列的 templet 会渲染「在线结算」——我们一个字都不写
    expect(cell(17)).not.toContain('在线结算')
    expect(cell(4)).not.toContain('扣点')
  })

  it('第 18 列「是否已申请结算」（⭐D-0365）：有申请 ⇒ 对标「已申请」（红），无 ⇒ 「未申请」（绿）', () => {
    // 产物逐字照对标 `myemp.js` L731-740（文案出处 = 同文件 L341-342 的字典）：
    // `<span style="color:#E03636">已申请</span>` / `<span style="color:#5FB878">未申请</span>`。
    expect(cell(18, ROW_APPLIED)).toBe('<span style="color:#E03636">已申请</span>')
    expect(cell(18)).toBe('<span style="color:#5FB878">未申请</span>')
    // ⚠️ 判据是 `is not None` 语义：`null` = 没申请（或申请已撤回）⇒ 未申请；
    //    空串不是这一列的合法值（后端 ISO 串或 null），不必另测。
  })

  it('用户名为 `null`（绩效行还没归属人）⇒ 空 span，不显示 `null`', () => {
    expect(cell(1, { ...ROW, username: null })).toBe('<span></span>')
  })

  it('用户输入被转义（`templet` 的返回值被 layui 当 HTML 插入）', () => {
    expect(cell(10, { ...ROW, project_text: '<b>注入</b>' })).toBe(
      '<span>&lt;b&gt;注入&lt;/b&gt;</span>',
    )
    expect(cell(1, { ...ROW, username: '<img src=x>' })).toBe(
      '<span>&lt;img src=x&gt;</span>',
    )
    expect(cell(9, { ...ROW, game_name: '<i>x</i>' })).toBe('&lt;i&gt;x&lt;/i&gt;')
  })

  it('订单状态列（下标 11）：`.lableb.setdlsta` 徽标，文案取后端词表、色取**本页**色表', () => {
    const html = cell(11, { ...ROW, status: 'WAITING_GRAB' })
    expect(html).toContain('class="lableb setdlsta"')
    expect(html).toContain('href="javascript:void(0)"')
    expect(html).toContain('data-id="20260920213215233029"')
    expect(html).toContain('title="当前订单状态：待抢单"')
    expect(html).toContain('>待抢单</a>')
    expect(html).toContain('background-color:#585858;')
    // 业务方规则：同一个状态在任何页面同一文案（对标这一页写「待上号」，我们不复刻那个口径）
    expect(html).not.toContain('待上号')
  })

  it('状态色表逐条取**本页**对标 `orderSta` templet 的 switch（与 order-myorderlist 不同）', () => {
    expect(STATUS_BADGE_COLORS).toEqual({
      WAITING_GRAB: '#585858',
      IN_PROGRESS: '#219227',
      PENDING_REVIEW: '#ff6a00',
      COMPLETED: '#0094ff',
      PAUSED: '#FF5722',
      ISSUE: '#ff0000',
      REFUNDED: '#efaaaa',
    })
    // ⚠️ 与「我的订单」页的同一状态取值不同（那一页 PAUSED=#efaaaa、ISSUE=#FF5722）
    expect(STATUS_BADGE_COLORS['PAUSED']).toBe('#FF5722')
    expect(STATUS_BADGE_FALLBACK).toBe('#b0aeae;')
  })

  it('状态列：未知取值 → 原样文案 + 对标那个**带尾分号**的默认色', () => {
    const html = cell(11, { ...ROW, status: 'WEIRD' })
    expect(html).toContain('>WEIRD</a>')
    expect(html).toContain('#b0aeae;')
  })

  it('结算状态列（下标 15）：已结账取 `#9ACD32`，未结账取 `#CDC8B1`，文案是后端真值', () => {
    expect(cell(15, { ...ROW, settle_status: '已结账' })).toContain('background-color:#9ACD32;')
    expect(cell(15, { ...ROW, settle_status: '已结账' })).toContain('>已结账</a>')
    expect(cell(15, { ...ROW, settle_status: '未结账' })).toContain('background-color:#CDC8B1;')
    expect(cell(15, { ...ROW, settle_status: '未结账' })).toContain('>未结账</a>')
    // 对标这一列的 `data-id` 是 `res.empId`（**绩效行主键**）⇒ 填 `id`，**不是**订单号。
    // 这一条与下标 6/11（订单 id 位一律 `order_no`）是两种身份，别混。
    expect(cell(15)).toContain('data-id="7"')
    expect(cell(15)).not.toContain(`data-id="${ROW.order_no}"`)
  })
})

describe('筛选：5 个控件全部客户端（`/my-orders/` 不读业务筛选参数）', () => {
  it('`applyFilters`：结算状态 / 订单状态 / 游戏 / 两个搜索键都是 AND', () => {
    const rows = [ROW, ROW_SETTLED]
    expect(applyFilters(rows, NO_FILTER).map((r) => r.order_no)).toEqual([
      '20260920213215233029',
      '20260920213215233030',
    ])
    expect(
      applyFilters(rows, { ...NO_FILTER, settleStatus: '已结账' }).map((r) => r.order_no),
    ).toEqual(['20260920213215233030'])
    expect(
      applyFilters(rows, { ...NO_FILTER, orderStatuses: ['IN_PROGRESS'] }).map((r) => r.order_no),
    ).toEqual(['20260920213215233029'])
    expect(
      applyFilters(rows, { ...NO_FILTER, game: '原神' }).map((r) => r.order_no),
    ).toEqual(['20260920213215233029'])
    // 子串、大小写不敏感（搜索1 按 order_no）
    expect(applyFilters(rows, { ...NO_FILTER, key1: '213215233030' }).map((r) => r.order_no)).toEqual(
      ['20260920213215233030'],
    )
    // 搜索2 按 project_text
    expect(applyFilters(rows, { ...NO_FILTER, key2: '忘却' }).map((r) => r.order_no)).toEqual([
      '20260920213215233030',
    ])
    // AND：两个条件各命中一行 ⇒ 结果为空
    expect(
      applyFilters(rows, { ...NO_FILTER, game: '原神', key2: '忘却' }).length,
    ).toBe(0)
  })

  it('点「搜索」按 `#key1` 真的收窄渲染行（`table.reload` 只拿到命中的那一行）', async () => {
    const root = await mountPage([ROW, ROW_SETTLED])
    const key1 = root.querySelector<HTMLInputElement>('#key1')
    expect(key1).not.toBeNull()
    key1!.value = '20260920213215233030'
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    expect(LAYUI.reload).toHaveBeenCalledWith('tableList', {
      data: [ROW_SETTLED],
      page: { curr: 1, limit: 20 },
    })
  })

  it('「结算状态 = 已结算」那一档（option value `Y`）收窄到已结账行', async () => {
    const root = await mountPage([ROW, ROW_SETTLED])
    const select = root.querySelector<HTMLSelectElement>('#settingSta')
    // 证据件：`<option value="Y">已结算</option>`（文案是对标的，值映射到后端 `已结账`）
    expect(select?.querySelector('option[value="Y"]')).not.toBeNull()
    select!.value = 'Y'
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    expect(LAYUI.reload).toHaveBeenCalledWith('tableList', {
      data: [ROW_SETTLED],
      page: { curr: 1, limit: 20 },
    })
  })

  it('「选择游戏」下拉变化即时收窄（对标 `select(selectGame)` 的时机）', async () => {
    const root = await mountPage([ROW, ROW_SETTLED])
    const select = root.querySelector<HTMLSelectElement>('#gameName')
    // 证据件里 value `21701` 的文案是「原神」
    const option = select?.querySelector<HTMLOptionElement>('option[value="21701"]')
    expect(option?.textContent).toBe('原神')
    select!.value = '21701'
    LAYUI.formHandlers.get('select(selectGame)')?.()
    expect(LAYUI.reload).toHaveBeenCalledWith('tableList', {
      data: [ROW],
      page: { curr: 1, limit: 20 },
    })
  })

  /**
   * 以下几条走的是订单状态筛选的**生产路径**：页面读的是 `getFormSelects()`（顶层替身见文件说明），
   * 不是原生 `<select>` 兜底。评审实测过：只测兜底时「档位翻错枚举」这类变异杀不掉；
   * 这几条把它们钉死（期望值全是**字面枚举名**）。
   */
  it('formSelects 真路径：`待抢单` → `WAITING_GRAB`（第一档，已与其它页统一），只剩那一行', async () => {
    await mountPage([ROW, ROW_SETTLED, ROW_WAITING])
    FS.value.mockReturnValue(['待抢单'])
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    expect(FS.value).toHaveBeenCalledWith('orderSta', 'val')
    expect(LAYUI.reload).toHaveBeenLastCalledWith('tableList', {
      data: [ROW_WAITING],
      page: { curr: 1, limit: 20 },
    })
  })

  it('formSelects 真路径：证据件 7 档**逐档**翻成对应的后端枚举名（字面值）', async () => {
    await mountPage(STATUS_ROWS)
    STATUS_CASES.forEach(([label], i) => {
      FS.value.mockReturnValue([label])
      LAYUI.formHandlers.get('submit(seachBtn)')?.()
      expect(LAYUI.reload).toHaveBeenLastCalledWith('tableList', {
        data: [STATUS_ROWS[i]],
        page: { curr: 1, limit: 20 },
      })
    })
  })

  it('formSelects 真路径：多选（`已完成` + `代练中`）取并集', async () => {
    await mountPage([ROW, ROW_SETTLED, ROW_WAITING])
    FS.value.mockReturnValue(['已完成', '代练中'])
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    expect(LAYUI.reload).toHaveBeenLastCalledWith('tableList', {
      data: [ROW, ROW_SETTLED],
      page: { curr: 1, limit: 20 },
    })
  })

  it('拿不到 formSelects 实例时退回原生 `<select>`（兜底那支也要钉）', async () => {
    const root = await mountPage([ROW, ROW_SETTLED, ROW_WAITING])
    FS.current = undefined
    const sel = root.querySelector<HTMLSelectElement>('select[xm-select="orderSta"]')
    expect(sel).not.toBeNull()
    const opt = sel!.querySelector<HTMLOptionElement>('option[value="已完成"]')
    expect(opt).not.toBeNull()
    opt!.selected = true
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    expect(LAYUI.reload).toHaveBeenLastCalledWith('tableList', {
      data: [ROW_SETTLED],
      page: { curr: 1, limit: 20 },
    })
  })

  it('换筛选只回第 1 页，但**保留**用户选的「条/页」（取 `table.getOptions` 的实时值）', async () => {
    const root = await mountPage([ROW, ROW_SETTLED])
    LAYUI.getOptions.mockReturnValue({ page: { limit: 50 } })
    root.querySelector<HTMLInputElement>('#key1')!.value = '213215233030'
    LAYUI.formHandlers.get('submit(seachBtn)')?.()
    expect(LAYUI.getOptions).toHaveBeenCalledWith('tableList')
    expect(LAYUI.reload).toHaveBeenLastCalledWith('tableList', {
      data: [ROW_SETTLED],
      page: { curr: 1, limit: 50 },
    })
  })
})

describe('⭐ D-0365 日期三控件：服务端筛选，改了即重取', () => {
  it('挂载时把运行期默认区间写进两个日期框（证据件里的死值被覆盖）', async () => {
    const root = await mountPage()
    const s = root.querySelector<HTMLInputElement>('#sDate')
    const e = root.querySelector<HTMLInputElement>('#eDate')
    // 字面值来自 dateRange 替身（文件头）——不写实现派生值（E-0024）。
    expect(s?.value).toBe('2026-05-02 00:00:00')
    expect(e?.value).toBe('2026-11-02 23:59:59')
  })

  it('`#dateType` 换档 → 重新 GET，`date_type` 是映射值（证据件数字档 → 枚举，逐档钉值）', async () => {
    const root = await mountPage([ROW])
    const sel = root.querySelector<HTMLSelectElement>('#dateType')
    expect(sel).not.toBeNull()
    for (const [value, expected] of [
      ['10', 'claimed'],
      ['20', 'submitted'],
      ['25', 'created'],
      ['30', 'settled'],
    ] as const) {
      mockedRequest.mockClear()
      sel!.value = value
      LAYUI.formHandlers.get('select()')?.({ elem: sel })
      await vi.waitFor(() => {
        expect(mockedRequest).toHaveBeenCalledWith({
          method: 'get',
          url: '/api/business/my-orders/',
          // ⚠️ 两个日期框里有挂载时写入的默认区间 ⇒ 两端照发（dateFetchParams 的行为）。
          params: { page: 1, page_size: 200, ...DEFAULT_DATE_PARAMS, date_type: expected },
        })
      })
      expect(DATE_TYPE_MAP[value]).toBe(expected)
    }
  })

  it('改 `#sDate` → 重新 GET，`date_from` 取**日期段**（不是整串 datetime）', async () => {
    const root = await mountPage([ROW])
    const s = root.querySelector<HTMLInputElement>('#sDate')
    s!.value = '2026-01-15 08:30:00'
    s!.dispatchEvent(new Event('change', { bubbles: true }))
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenLastCalledWith({
        method: 'get',
        url: '/api/business/my-orders/',
        params: {
          page: 1,
          page_size: 200,
          date_type: 'claimed',
          date_from: '2026-01-15',
          date_to: '2026-11-02',
        },
      })
    })
  })

  it('日期框非空但取不出日期 ⇒ 弹一句「不是可识别的日期」、那一端参数不发、GET 照发', async () => {
    const root = await mountPage([ROW])
    const s = root.querySelector<HTMLInputElement>('#sDate')
    s!.value = '不是日期'
    s!.dispatchEvent(new Event('change', { bubbles: true }))
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenLastCalledWith({
        method: 'get',
        url: '/api/business/my-orders/',
        params: { page: 1, page_size: 200, date_type: 'claimed', date_to: '2026-11-02' },
      })
    })
    expect(LAYUI.msg).toHaveBeenCalledWith(
      `「接手时间」${UNPARSABLE_DATE_TEXT}`,
      { time: 1500 },
    )
  })

  it('结算状态下拉走**同一个**空 `lay-filter` 的 `select()` 通道，不触发重取', async () => {
    const root = await mountPage()
    const before = mockedRequest.mock.calls.length
    LAYUI.formHandlers.get('select()')?.({ elem: root.querySelector('#settingSta') })
    expect(mockedRequest.mock.calls.length).toBe(before)
    expect(LAYUI.msg).not.toHaveBeenCalled()
  })
})

describe('⭐ D-0365 工具栏三颗真功能 + 一颗「尚未开放」', () => {
  it('「刷新列表」是真的：重新发一次 GET（筛选值不清空）', async () => {
    await mountPage()
    const before = mockedRequest.mock.calls.length
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'refresh' })
    await vi.waitFor(() => expect(mockedRequest.mock.calls.length).toBeGreaterThan(before))
  })

  it('「申请结算」空选 ⇒ 对标同款提示（`请选中要操作的记录`），零请求', async () => {
    await mountPage()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'applicationBtn' })
    expect(mockedRequest).not.toHaveBeenCalledWith(
      expect.objectContaining({ url: '/api/business/my-orders/settle-requests/' }),
    )
    expect(LAYUI.msg).toHaveBeenCalledWith(EMPTY_SELECTION_TEXT, { time: 1500 })
  })

  it('「申请结算」勾选行 ⇒ POST settle-requests（载荷是绩效 id），成功汇总 + 重取', async () => {
    await mountPage([ROW, ROW_SETTLED])
    LAYUI.checkStatus.mockReturnValue({ data: [ROW, ROW_SETTLED] })
    mockedRequest.mockImplementation(async (config: { method?: string; url?: string }) => {
      if (config.url?.includes('settle-requests')) {
        return {
          items: [
            { performance_id: 7, ok: true, code: 0, msg: '', settle_request: {} },
            { performance_id: 8, ok: true, code: 0, msg: '', settle_request: {} },
          ],
          early_settlement: { used: 2, limit: 3 },
        }
      }
      return { items: [ROW, ROW_SETTLED], total: 2, page: 1, page_size: 200 }
    })
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'applicationBtn' })
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenCalledWith({
        method: 'post',
        url: '/api/business/my-orders/settle-requests/',
        data: { performance_ids: [7, 8] },
      })
    })
    // 成功汇总（我方自拟的这句）走 1500ms 的 msg；成功 ≥1 条才重取。
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('已提交 2 条提前结算申请', { time: 1500 })
    })
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenLastCalledWith({
        method: 'get',
        url: '/api/business/my-orders/',
        params: { page: 1, page_size: 200, ...DEFAULT_DATE_PARAMS },
      })
    })
  })

  it('「申请结算」混批 ⇒ 失败 msg **原样**合并（后端文案不翻译）、6000ms、有成功才重取', async () => {
    await mountPage([ROW, ROW_SETTLED])
    LAYUI.checkStatus.mockReturnValue({ data: [ROW, ROW_SETTLED] })
    mockedRequest.mockImplementation(async (config: { method?: string; url?: string }) => {
      if (config.url?.includes('settle-requests')) {
        return {
          items: [
            { performance_id: 7, ok: true, code: 0, msg: '', settle_request: {} },
            {
              performance_id: 8,
              ok: false,
              code: 1035,
              msg: '该绩效已申请过提前结算，请等待财务处理',
            },
          ],
          early_settlement: { used: 1, limit: 3 },
        }
      }
      return { items: [ROW, ROW_SETTLED], total: 2, page: 1, page_size: 200 }
    })
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'applicationBtn' })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '已提交 1 条提前结算申请；该绩效已申请过提前结算，请等待财务处理',
        { time: 6000 },
      )
    })
    // 有成功 ⇒ 重取发生在提示之后（调用序列里有 GET）。
    await vi.waitFor(() => {
      expect(mockedRequest).toHaveBeenLastCalledWith(
        expect.objectContaining({ method: 'get', url: '/api/business/my-orders/' }),
      )
    })
  })

  it('「申请结算」全失败 ⇒ 不重取（数据没变），失败文案 6000ms', async () => {
    await mountPage([ROW])
    LAYUI.checkStatus.mockReturnValue({ data: [ROW] })
    const getCalls = () =>
      mockedRequest.mock.calls.filter((c) => c[0] && (c[0] as { url?: string }).url?.endsWith('my-orders/')).length
    mockedRequest.mockImplementation(async (config: { method?: string; url?: string }) => {
      if (config.url?.includes('settle-requests')) {
        return {
          items: [
            { performance_id: 7, ok: false, code: 1036, msg: '提前结算申请额度已用完（3/3）' },
          ],
          early_settlement: { used: 3, limit: 3 },
        }
      }
      return { items: [ROW], total: 1, page: 1, page_size: 200 }
    })
    const before = getCalls()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'applicationBtn' })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith('提前结算申请额度已用完（3/3）', { time: 6000 })
    })
    // 静一拍再数：没有第二次 GET（全失败不 reload）。
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(getCalls()).toBe(before)
  })

  it('「导出记录」⇒ GET export（当前筛选 + 日期参数打包），Blob 落下载（不看勾选）', async () => {
    await mountPage([ROW])
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, writable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, writable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const blob = new Blob(['xlsx-bytes'], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    mockedHttpGet.mockResolvedValue({
      data: blob,
      headers: { 'content-disposition': 'attachment; filename="my-orders-20261102.xlsx"' },
    } as never)
    // 对标空选**不拦**导出（exportOrder 对空选照发 seachParam）⇒ 不勾选也应导出。
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'exportBtn' })
    await vi.waitFor(() => expect(mockedHttpGet).toHaveBeenCalled())
    expect(mockedHttpGet).toHaveBeenCalledWith(
      '/api/business/my-orders/export',
      expect.objectContaining({
        responseType: 'blob',
        // 日期默认区间与首屏 GET 同一份字面值（DEFAULT_DATE_PARAMS，见文件头）。
        params: expect.objectContaining({ ...DEFAULT_DATE_PARAMS }),
      }),
    )
    await vi.waitFor(() => expect(clickSpy).toHaveBeenCalled())
    expect(createObjectURL).toHaveBeenCalledWith(blob)
    clickSpy.mockRestore()
    Object.defineProperty(URL, 'createObjectURL', { value: undefined, writable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: undefined, writable: true })
  })

  it('「导出记录」失败信封（HTTP 200 + code 1000）⇒ 弹后端 msg 的 6000ms', async () => {
    await mountPage([ROW])
    const blob = new Blob([JSON.stringify({ code: 1000, msg: '本次导出 2 行，超过上限 1 行，请先收窄筛选条件', data: null })], {
      type: 'application/json',
    })
    mockedHttpGet.mockResolvedValue({ data: blob, headers: {} } as never)
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'exportBtn' })
    await vi.waitFor(() => {
      expect(LAYUI.msg).toHaveBeenCalledWith(
        '本次导出 2 行，超过上限 1 行，请先收窄筛选条件',
        { time: 6000 },
      )
    })
  })

  it('「同步结算状态到我的系统」仍是唯一的「尚未开放」（D-0365 改准：另两颗已接）', async () => {
    await mountPage()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'snycSettStatusBtn' })
    expect(LAYUI.msg).toHaveBeenCalledWith('「同步结算状态到我的系统」尚未开放', { time: 1500 })
    // ⛔（D-0365）`applicationBtn` / `exportBtn` 不弹「尚未开放」。
    LAYUI.msg.mockClear()
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'applicationBtn' })
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'exportBtn' })
    expect(LAYUI.msg).not.toHaveBeenCalledWith(expect.stringContaining('尚未开放'))
  })

  it('layui 内置「筛选列」图标（`LAYTABLE_COLS`）**不弹**「尚未开放」，也不发请求', async () => {
    // 本页 `defaultToolbar:['filter']` ⇒ 右上角那颗图标是**真功能**，layui 会用一个
    // `toolbar(id)` 事件把它的点击交上来（事件名 `LAYTABLE_COLS`，见 `.tools/parity.mjs:255`）。
    // 对标那个 handler 的 switch **没有 default 分支** ⇒ 我们也不能用 `else` 兜底弹提示。
    await mountPage()
    const before = mockedRequest.mock.calls.length
    LAYUI.tableHandlers.get('toolbar(tableList)')?.({ event: 'LAYTABLE_COLS' })
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('「尚未开放」白名单恰好只剩「同步结算状态」一颗（D-0365 改准）', () => {
    expect(TOOLBAR_NOT_OPEN_EVENTS).toEqual(['snycSettStatusBtn'])
  })
})

describe('`#total` 统计句：文案照对标、数字恒 0（不编聚合）', () => {
  it('`table.render` 的 `done` 把 `TOTAL_TEXT` 写进工具栏模板里的空 `#total`', async () => {
    const root = await mountPage()
    const opts = LAYUI.render.mock.calls[0]?.[0] as { done?: () => void }
    opts.done?.()
    const total = root.querySelector('#total')
    expect(total).not.toBeNull()
    expect(total!.textContent).toBe(TOTAL_TEXT)
  })
})

describe('T5：行内订单号蓝链 ⇒ 原地开层（对标 `myemp.js` L832-843）', () => {
  /** 点那一列的真蓝链，返回 `layer.open` 的入参（`orderNo` 可按用例改写）。 */
  async function clickOrderLink(orderNo: string = ROW.order_no): Promise<Record<string, unknown>> {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildRuntimeCols()[6]?.templet?.({ ...ROW, order_no: orderNo }) ?? ''
    root.appendChild(wrap)
    const link = root.querySelector<HTMLElement>('a.orderdetails')
    expect(link, '订单号 templet 没有产出 a.orderdetails').not.toBeNull()
    // 挂载期的首屏取数与「开层」无关 ⇒ 清一次，下面的判据才是钉**点击这一下**。
    LAYUI.open.mockClear()
    navigate.mockClear()
    link!.click()
    expect(LAYUI.open, '点了单号蓝链没有开层').toHaveBeenCalledTimes(1)
    return LAYUI.open.mock.calls[0]![0] as Record<string, unknown>
  }

  it('⭐ 层参数逐字（title / shade / shadeClose / type / skin / anim / area = **本页自己的** `60%×92%`）', async () => {
    const opts = await clickOrderLink()
    // 对标：`title: orderid + i18nDataList.viewOrderDetails`，zh-CN 的 `viewOrderDetails`
    // = 「订单详情」（`myemp.js` L352 原文，抄在上面）。
    expect(opts['title']).toBe('20260920213215233029订单详情')
    expect(opts['shade']).toEqual([0.3, '#333'])
    expect(opts['shadeClose']).toBe(true)
    expect(opts['type']).toBe(2)
    expect(opts['skin']).toBe('layui-layer-lan')
    expect(opts['anim']).toBe(8)
    // ⛔ **本页自己的值**（`myemp.js` L840）。改成任何别页的值（`85%×92%` / `90%×92%` /
    //    `1125px|80%×90%`）本条必须红。
    expect(opts['area']).toEqual(['60%', '92%'])
    // ⚠️ **对标这个函数体里没有 `maxmin` 这个键** ⇒ 我们也不许写（写了就是凭空多一个参数，
    //    且会把「与对标同默认」变成「我们显式指定」）。
    expect('maxmin' in opts).toBe(false)
  })

  it('⭐ `content` = 我们自己的复刻页（`page=order-orderview` + `order_no` = 那一行的单号），且**不再跳路由**', async () => {
    const opts = await clickOrderLink()
    // 尖括号里的 `<BASE_URL>` 在本工程是 `/`（vitest 的 base 与 dev 相同）。
    expect(opts['content']).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
    )
    const opts2 = await clickOrderLink('20260101010101010101')
    expect(opts2['content']).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260101010101010101',
    )
    // 负向对照：改了之后**一次都没有**跳路由。
    expect(navigate).not.toHaveBeenCalled()
  })

  it('⭐ 基址用 `import.meta.env.BASE_URL`（⛔ 不写死 `/`）：把它 stub 成 `/sub/` ⇒ `content` 跟着变', async () => {
    // ⚠️ **先挂载、再 stub**：挂载期取证据件的 `fetch` 也走 `BASE_URL`，提前改会让那条路径取错文件。
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildRuntimeCols()[6]?.templet?.(ROW) ?? ''
    root.appendChild(wrap)
    const link = wrap.querySelector<HTMLElement>('a.orderdetails')!
    vi.stubEnv('BASE_URL', '/sub/')
    try {
      LAYUI.open.mockClear()
      link.click()
      // 期望值是**字面值**（E-0024）⇒ **不是自指断言**：把实现的 `${import.meta.env.BASE_URL}`
      // 写死成 `'/'`，本条会红（见收口记录的变异 ⑫）。
      expect((LAYUI.open.mock.calls[0]![0] as Record<string, unknown>)['content']).toBe(
        '/sub/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
      )
    } finally {
      vi.unstubAllEnvs()
    }
  })

  it('⭐ 开层**不发任何 API 请求**（数据由层里的 island 页自己取；本页只开层）', async () => {
    // ⚠️ 必须**先挂载完**再量：`mountPage()` 自己会发首屏取数——把 `before` 量在挂载之前，
    //    会把「挂载的请求」算到「点击」头上（本用例第一版就是这么假红的）。
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildRuntimeCols()[6]?.templet?.(ROW) ?? ''
    root.appendChild(wrap)
    const link = root.querySelector<HTMLElement>('a.orderdetails')!
    const before = mockedRequest.mock.calls.length
    link.click()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })

  it('⛔ 开层前的**单号形状闸**：非 20 位数字 / 带标记的 `data-order` ⇒ **不开层** + 固定文案提示 + **零请求**', async () => {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML = buildRuntimeCols()[6]?.templet?.(ROW) ?? ''
    root.appendChild(wrap)
    const link = root.querySelector<HTMLElement>('a.orderdetails')!
    // 四种「形状不对」：短一位 / 多一位 / 非数字 / 带可执行形态的标记串。
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

  it('⭐ 2026-10-29（fix/taker-web-order-detail）：蓝链开层**不判详情权限**——层 URL 不带任何模式标志，打手点开由 order-orderview 页内自判进受限模式', async () => {
    // 本页挂在「我的收入」（`/my-orders/` 载荷）上，打手本来就能到；蓝链开的是
    // `page=order-orderview` 岛页——有没有 `order.view_detail`/`order.view_content`
    // 由那一页挂载期自己探 `/api/auth/me` 判（`@/layui-island/perms` 的探针先例）。
    // 本条钉住：入口 URL 恒定，**没有** mode / restricted 之类的标志可传。
    const opts = await clickOrderLink()
    expect(opts['content']).toBe(
      '/layui-island/page.html?page=order-orderview&order_no=20260920213215233029',
    )
    expect(String(opts['content'])).not.toMatch(/mode|restricted|limited|view=/i)
    // ⚠️ 本文件的 beforeEach 不清 `LAYUI.open` ⇒ 收尾清一次，别渗进下一条
    //    「两颗状态徽标不接点击」的 not.toHaveBeenCalled()。
    LAYUI.open.mockClear()
  })
})

describe('行内订单号蓝链的**其它**出口没有回归', () => {
  it('两颗状态徽标**不接**点击（对标 `myemp.js` 自己也没有它们的处理器）', async () => {
    const root = await mountPage()
    const wrap = document.createElement('div')
    wrap.innerHTML =
      (buildRuntimeCols()[11]?.templet?.(ROW) ?? '') +
      (buildRuntimeCols()[15]?.templet?.(ROW) ?? '')
    root.appendChild(wrap)
    const before = mockedRequest.mock.calls.length
    root.querySelector<HTMLElement>('a.setdlsta')?.click()
    root.querySelector<HTMLElement>('a.setsta')?.click()
    expect(navigate).not.toHaveBeenCalled()
    expect(LAYUI.open).not.toHaveBeenCalled()
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(mockedRequest.mock.calls.length).toBe(before)
  })
})

describe('证据件「订单状态」下拉：第一档与其它页统一为「待抢单」（有意偏差，见文件头登记第 13 条）', () => {
  it('`select[name=orderSta]` 那一档是 `<option value="待抢单">待抢单</option>`，不含对标的「待上号」', () => {
    // ⚠️ 判据**只取这一段 `<select>`**：证据件头部那几条偏差登记里就写着「待上号」
    //（它记载的是「我们把对标原文的哪一句改掉了」），整份文件级别的 not 断言会误伤。
    expect(STA_SELECT).not.toBe('')
    expect(STA_SELECT).toContain('<option value="待抢单">待抢单</option>')
    expect(STA_SELECT).not.toContain('待上号')
    // 其余 6 档一字未动（改动只限第一档）
    for (const label of ['代练中', '待验收', '已完成', '问题单', '已退单', '已暂停']) {
      expect(STA_SELECT).toContain(`<option value="${label}">${label}</option>`)
    }
  })
})
