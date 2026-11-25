// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【交易订单操作记录管理列表】复刻页的**门禁**。
 *
 * ## 为什么必须有「驱动真交互」的用例（`AGENTS.md` §5-G / E-0061 / E-0067）
 *
 * 本页 2026-10-04（`feat/trade-action-log`）**脱离了 `artifactPage`**：那个共享渲染器在**捕获阶段**
 * 把所有 `[lay-event]` 的点击收口成「尚未开放」并 `stopPropagation` ⇒ 在它下面接
 * `table.on('toolbar(…)')` 是死路（本页工具栏那颗按钮正是 `lay-event="refresh"`）。
 * 代价：`typecheck` 看不见 DOM 选择器对不对、`lint` 看不见类名里多了个点、`parity` 在空表时
 * 看不到表体 ⇒ **本文件是唯一防线**，而「点了没反应」是**静默**的。
 *
 * 所以下面 C 组由 `table.on(...)` / `form.on(...)` **登记的那个真回调**驱动（= 真人点击时 layui
 * 会调的函数），列表取数走真 `table.render` 的那份配置；两个行内链接则用**列的 `templet` 真产物**
 * 造 DOM 再 `.click()`（连「templet 写哪个 data 属性」与「处理器读哪个属性」一起钉住）。
 *
 * ## 三条 island 陷阱（§5-G）在本文件里各有判据
 *
 * ① 类名与选择器分两个常量 —— 页里是 `TABLE_ELEM`（`'#tableList'`）与 `TABLE_ID`（`'tableList'`），
 *    本文件用「`table.reload` 的第一个实参必须是 `TABLE_ID`」把它钉住（传 `#id` 是静默 no-op）；
 * ② 以 `(` 开头的语句前置分号 —— 页代码风格，由 `pnpm lint` 守；
 * ③ **先 `await bootstrapIsland(...)` 再塞标记**、而 `initIslandElements(layui)` 留在塞标记**之后**
 *    —— 本文件用 `LAYUI.state` 两个快照把它钉住。
 *
 * ## 既有 5 条断言一条不删
 *
 * A 组是改造前就在的那 5 条（表头/列宽/`sort`/两张表的渲染参数）。本线只按 spec 把**变化了的
 * 那部分**改准（`订单标题` → `变更内容`；`SPEC.tables[n]` → `LEFT_TABLE_OPTIONS` /
 * `RIGHT_TABLE_OPTIONS`），断言面**没有放宽**（仍是字面值、仍是恰好序列）。
 */
const EVIDENCE = fs.readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/order-tradingorderactionloglist.html',
  ),
  'utf8',
)

/**
 * layui 的**可控替身**。真 layui 在 jsdom 里加载不了（证据：`order-orderrecycle.spec.ts` 同形）。
 * `vi.hoisted` 是因为 `vi.mock` 的工厂会被提升到 import 之前。
 */
const LAYUI = vi.hoisted(() => {
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const msgs: string[] = []
  /** 陷阱 ③ 的两个快照：那两次调用发生时，页面的表格标记**在不在** document 里。 */
  const state = { bootstrapSawMarkup: true, initSawMarkup: false }
  return {
    formHandlers,
    tableHandlers,
    msgs,
    state,
    formSelects: {
      render: vi.fn(),
      data: vi.fn(),
      value: vi.fn((): unknown => ''),
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
      msg: vi.fn((t: string) => {
        msgs.push(t)
      }),
      open: vi.fn(),
    },
    laydate: { render: vi.fn() },
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => {
      // ⚠️ 陷阱 ③ 前半：`bootstrapIsland()` 跑的时候页面标记**还不该在** document 里。
      LAYUI.state.bootstrapSawMarkup = document.querySelector('#tableList') !== null
      return LAYUI
    }) as unknown as typeof actual.bootstrapIsland,
    initIslandElements: vi.fn(() => {
      // ⚠️ 陷阱 ③ 后半：`initIslandElements(layui)` 必须在**塞完标记之后**才被调用。
      LAYUI.state.initSawMarkup = document.querySelector('#tableList') !== null
    }),
  }
})

// 证据件走替身（真实现是运行期 `fetch('replica/…')`）。
vi.mock('@/layui-island/config-replica', () => ({
  loadConfigArtifact: async () => EVIDENCE,
}))

vi.mock('@/layui/vendor', () => ({
  initFormSelects: vi.fn(),
  getFormSelects: () => LAYUI.formSelects,
}))

import {
  ACTION_ALL_VALUE,
  ORDER_ACTION_LOGS_URL,
  toOrderActionLogParams,
  type OrderActionLogActionOption,
  type OrderActionLogRow,
  type OrderActionLogSummaryRow,
} from '@/api/orderActionLogs'
import page, {
  buildCols,
  COLS2_FOR_TEST,
  COLS_FOR_TEST,
  defaultActionLogDateRange,
  formatChange,
  LEFT_TABLE_OPTIONS,
  PAGE_LIMITS,
  RIGHT_TABLE_OPTIONS,
  SORT_FIELD,
  TABLE2_ELEM,
  TABLE_ELEM,
  TABLE_FILTER,
  TABLE_ID,
  TOOLBAR_ELEM,
  toLogRow,
  toOrderActionLogOrdering,
  toOrderActionLogSearchParams,
  toSummaryRow,
  type ActionLogCol,
  type LogRow,
} from '../order-tradingorderactionloglist'

/** 合成夹具（PII 纪律：单号只是 20 位数字的**形状**，游戏/昵称带「测试…甲」的合成后缀）。 */
const ROW: OrderActionLogRow = {
  order_no: '20261004000000000001',
  source_order_no: 'TB-20261004000000000002',
  game_name: '测试游戏甲',
  from_value: '代练中',
  to_value: '待验收',
  action: 'order.transition',
  action_label: '更改状态',
  summary: '将订单 20261004000000000001 的状态从代练中改为待验收',
  actor: 'kefu_jia',
  actor_display: '测试昵称甲',
  created_at: '2026-10-04T12:30:45+08:00',
}

const SUMMARY: OrderActionLogSummaryRow[] = [
  {
    action: 'order.transition',
    action_label: '更改状态',
    actor: 'kefu_jia',
    actor_display: '测试昵称甲',
    count: 7,
  },
  {
    action: 'order.create',
    action_label: '录入订单',
    actor: 'kefu_jia',
    actor_display: '测试昵称甲',
    count: 3,
  },
]

/** 旧后端的形状（只有 `actors: string[]`）：部署窗口兼容用的反向夹具。 */
const ACTORS = ['kefu_jia', 'kefu_yi']

/**
 * 新后端的「操作人员」候选：`actor` = 登录账号、`display` = 账号名称。
 * ⚠️ 两者**不同形**（`kefu_jia` vs `测试昵称甲`），否则「文本用昵称、值用账号」这条判据会假绿。
 */
const ACTOR_OPTIONS = [
  { actor: 'kefu_jia', display: '测试昵称甲' },
  { actor: 'kefu_yi', display: '测试昵称乙' },
]

/**
 * 「操作动作」下拉的候选（**由后端随响应给**，spec §3-D3）。
 *
 * ⚠️ 这里只放**两条**合成候选、且顺序是**故意打乱**的（真实后端给的是字典序）：本页只负责
 * 原样渲染 —— 候选有几条、按什么序，都不该由前端决定（短名的唯一出处是后端
 * `apps/audit/actions.py::ORDER_ACTION_LABELS`）。
 */
const ACTIONS: OrderActionLogActionOption[] = [
  { code: 'order.transition', label: '更改状态' },
  { code: 'order.create', label: '录入订单' },
]

/** 一份完整的响应信封（形状 = spec §4.1）。 */
const ENVELOPE = {
  code: 0,
  msg: '',
  data: {
    items: [ROW],
    total: 41,
    page: 1,
    page_size: 20,
    summary: SUMMARY,
    actors: ACTORS,
    actor_options: ACTOR_OPTIONS,
    actions: ACTIONS,
  },
}

/** 对标筛选栏「操作动作」那 10 个**它自己的**动作词（证据件 L133-142）——本页一个都不许留。 */
const BENCHMARK_ACTION_LABELS = [
  '发单',
  '改成问题单',
  '改成异常单',
  '改成已暂停',
  '申请撤销',
  '取消撤销',
  '同意撤销',
  '改价改时',
  '验收订单',
  '改成已退单',
]

type RenderOpts = Record<string, unknown> & {
  cols?: ActionLogCol[][]
  data?: unknown
  page?: unknown
  request?: unknown
  toolbar?: unknown
  defaultToolbar?: unknown
  where?: Record<string, unknown>
  parseData?: (r: unknown) => { code: number; msg: string; count: number; data: LogRow[] }
  done?: () => void
}

/** 挂载整页。`root` 必须**在 document 里**——陷阱 ③ 的判据要查它。 */
async function mountPage(): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, { onNavigate: vi.fn() })
  return root
}

/** `table.render` 收到的、`elem` 匹配的那一份配置（**真正交给 layui 的出口**）。 */
function renderOptsFor(elem: string): RenderOpts {
  const call = LAYUI.table.render.mock.calls.find(
    ([opts]) => (opts as { elem?: string }).elem === elem,
  )
  expect(call, `table.render 没有渲染 ${elem}`).toBeTruthy()
  return (call as unknown[])[0] as RenderOpts
}

/** 最近一次 `table.reload` 的入参；没有就当场红。⚠️ 第一个实参必须是实例 id（陷阱 ①）。 */
function lastReload(): [string, { where: Record<string, unknown>; page: { curr?: number } }] {
  const calls = LAYUI.table.reload.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  const call = calls[calls.length - 1] as unknown[]
  expect(call[0]).toBe(TABLE_ID)
  return call as [string, { where: Record<string, unknown>; page: { curr?: number } }]
}

/** 驱动左表的 `parseData` + `done`（layui 收到响应后的两次调用，顺序固定）。 */
function driveResponse(): { code: number; msg: string; count: number; data: LogRow[] } {
  const opts = renderOptsFor(TABLE_ELEM)
  expect(opts.parseData).toBeTypeOf('function')
  const parsed = opts.parseData!(ENVELOPE)
  opts.done!()
  return parsed
}

/** 取某一列（按 `title`）。 */
function colByTitle(cols: ActionLogCol[], title: string): ActionLogCol {
  const col = cols.find((c) => c.title === title)
  expect(col, `没有标题为「${title}」的列`).toBeTruthy()
  return col!
}

/** 把某一列的 `templet` 真产物变成 DOM 节点（这样「写哪个 data 属性」也被驱动到）。 */
function renderCell(col: ActionLogCol, row: LogRow): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = (col.templet as (d: LogRow) => string)(row)
  return host
}

/** 本文件所在目录（`…/pages/__tests__`）。 */
const HERE = path.dirname(fileURLToPath(import.meta.url))

/**
 * 「前端不镜像短名」那条网的**扫描面**（spec §10.6）：本页的 api 封装 **加页面模块本身**。
 * ⚠️ 原先只扫 `@/api/orderActionLogs` 的**导出** ⇒ 页面模块里那份镜像**整个在网外**
 * （评审 S3 的漏洞②：把镜像放进页面模块，41 条照样全绿）。
 *
 * ⚠️ **边界（如实登记，⛔ 别把它当「全仓无镜像」的证明）**：扫描面 = 下面这两个**文件**的
 * 代码文本 + 这两个模块**导出值**的形状 ⇒「放进第三个新模块」、或「模块内、不导出、键在运行期
 * 从**不含 `order.` 字面量**的片段拼出来」这两类**不在网内**（真出现时扩扫描面 / 扩模块）。
 */
const MIRROR_SCAN_FILES = [
  { label: 'api/orderActionLogs.ts', file: path.resolve(HERE, '../../../api/orderActionLogs.ts') },
  {
    label: 'pages/order-tradingorderactionloglist.ts',
    file: path.resolve(HERE, '../order-tradingorderactionloglist.ts'),
  },
]

/**
 * 源码里 `order.*` 形式的**字符串字面量**（spec §10.6）。
 *
 * ⚠️ **注释剥掉**：注释里引一个动作码是排障说明，不是镜像（本页与 api 模块的注释里各有一处）。
 * ⚠️ 逐字符扫描而不是正则切注释：字符串里不认注释、注释里不认字符串——两端都要，
 * 否则 `'//'` 这类内容会把后文截断（漏报）、注释里的假动作码会伪命中（假红）。
 * ⚠️ 这条判据**不是**「页面里不许出现任何 `order.` 字符串」：本页合法的 `action` 参数名、
 * `data.actions` 的用法与 `order-orderview` 这个岛页 key 都**没有** `order.` 这个点号形状，
 * 不受影响；被挡住的只有「把动作码写成字面量」这一类。
 */
function orderLiteralsIn(source: string): string[] {
  const found: string[] = []
  let i = 0
  while (i < source.length) {
    const ch = source[i]
    const next = source[i + 1]
    if (ch === '/' && next === '/') {
      const newline = source.indexOf('\n', i)
      i = newline === -1 ? source.length : newline + 1
      continue
    }
    if (ch === '/' && next === '*') {
      const end = source.indexOf('*/', i + 2)
      i = end === -1 ? source.length : end + 2
      continue
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      const quote = ch
      let value = ''
      let j = i + 1
      while (j < source.length && source[j] !== quote) {
        if (source[j] === '\\') {
          value += source[j + 1] ?? ''
          j += 2
          continue
        }
        value += source[j]
        j += 1
      }
      if (/^order\.[A-Za-z0-9_]+$/.test(value)) found.push(value)
      i = j + 1
      continue
    }
    i += 1
  }
  return found
}

/**
 * 一个模块命名空间里「像 `code → 短名` 镜像表」的**导出名**（spec §10.6：形状覆盖对象 / 数组 / `Map`）。
 *
 * ⚠️ `Map` 必须单独看 `keys()`：`Object.keys(new Map([...]))` 恒为 `[]`
 * ⇒ 只查 `Object.keys` 时「把镜像写成 `new Map([...])`」会**全绿**（评审 S3 的漏洞①）。
 */
function mirroredOrderCodes(ns: Record<string, unknown>, label: string): string[] {
  const isOrderCode = (value: unknown): boolean =>
    typeof value === 'string' && value.startsWith('order.')
  const hits: string[] = []
  for (const [name, value] of Object.entries(ns)) {
    if (value instanceof Map) {
      if ([...value.keys()].some(isOrderCode)) hits.push(`${label}.${name}`)
      continue
    }
    if (Array.isArray(value)) {
      const strings = value.flatMap((item) =>
        typeof item === 'string'
          ? [item]
          : item && typeof item === 'object'
            ? Object.values(item).filter((v): v is string => typeof v === 'string')
            : [],
      )
      if (strings.some(isOrderCode)) hits.push(`${label}.${name}`)
      continue
    }
    if (value && typeof value === 'object') {
      if (
        Object.entries(value).some(
          ([key, entry]) => isOrderCode(key) || isOrderCode(entry),
        )
      ) {
        hits.push(`${label}.${name}`)
      }
    }
  }
  return hits
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  // 事件处理器表挂在 `Map` 上，`clearAllMocks()` 清不掉——不清的话上一次挂载注册的处理器
  // 会留在表里，「页面是否注册了某事件」这类断言会靠残留**假通过**。
  LAYUI.formHandlers.clear()
  LAYUI.tableHandlers.clear()
  LAYUI.msgs.length = 0
  LAYUI.formSelects.value.mockReturnValue('')
  LAYUI.state.bootstrapSawMarkup = true
  LAYUI.state.initSawMarkup = false
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('A. 交易订单操作记录 复刻页的列声明与渲染参数（**既有断言，一条不删**）', () => {
  it('左表 7 列，顺序与文案对齐 TradingOrderActionLogList.js L59-85（第 4 列是本线的「变更内容」）', () => {
    expect(COLS_FOR_TEST.map((c) => c.title)).toEqual([
      '系统单号',
      '交易单号',
      '游戏名称',
      // ⚠️ 改造前这里是「订单标题」；业务方 R4 把对标没有的那个字段换成「改前 → 改后」
      //（spec §3-D5）⇒ 标题变了，**宽度/位置/对齐都不动**。
      '变更内容',
      '操作说明',
      '操作人员',
      '操作时间',
    ])
    expect(COLS_FOR_TEST.map((c) => c.width)).toEqual([150, 190, 100, 200, 100, 100, 130])
    expect(COLS_FOR_TEST.some((c) => c.type !== undefined)).toBe(false)
  })

  it('左表只有「操作时间」是 sort:true（L84），其余列的 `sort:false` 是默认值不写', () => {
    expect(COLS_FOR_TEST.filter((c) => c.sort === true).map((c) => c.field)).toEqual([SORT_FIELD])
    expect(SORT_FIELD).toBe('actionTime')
  })

  it('右表 3 列（操作/操作人/操作次数），仅「操作次数」sort:true（L174）', () => {
    expect(COLS2_FOR_TEST.map((c) => c.title)).toEqual(['操作', '操作人', '操作次数'])
    expect(COLS2_FOR_TEST.map((c) => c.width)).toEqual([100, 130, 100])
    expect(COLS2_FOR_TEST.filter((c) => c.sort === true).map((c) => c.field)).toEqual(['totalCount'])
  })

  it('左表渲染参数：toolbar/defaultToolbar(生效的 filter)/page(limit 20, limits 20/30/50)', () => {
    expect(LEFT_TABLE_OPTIONS.elem).toBe(TABLE_ELEM)
    expect(LEFT_TABLE_OPTIONS.toolbar).toBe(TOOLBAR_ELEM)
    expect(LEFT_TABLE_OPTIONS.defaultToolbar).toEqual(['filter'])
    expect(LEFT_TABLE_OPTIONS.page).toEqual({ limit: 20, limits: [...PAGE_LIMITS] })
    // ⛔ 钉**字面值**、不钉 `PAGE_LIMITS` 那个常量：拿常量比自己会让「常量被改成 [20]」照样绿
    //    （自指断言，E-0024）。`[20,30,50]` 是对标 `TradingOrderActionLogList.js` L89 的实测值。
    expect(LEFT_TABLE_OPTIONS.page).toEqual({ limit: 20, limits: [20, 30, 50] })
  })

  it('右表渲染参数：**无 toolbar / 无 defaultToolbar**、**page:false**（对齐 L169-181 的渲染形态）', () => {
    expect(RIGHT_TABLE_OPTIONS.elem).toBe(TABLE2_ELEM)
    expect(RIGHT_TABLE_OPTIONS.toolbar).toBeUndefined()
    expect(RIGHT_TABLE_OPTIONS.defaultToolbar).toBeUndefined()
    expect(RIGHT_TABLE_OPTIONS.page).toBe(false)
  })
})

describe('B. 「变更内容」列 = from_value → to_value（spec §3-D5）', () => {
  it('两侧都有值 ⇒ `改前 → 改后`（箭头两侧各一个空格）', () => {
    expect(formatChange('代练中', '待验收')).toBe('代练中 → 待验收')
  })

  it('**两侧都空 ⇒ 空串**（⛔ 不写 `—`、⛔ 不复用 summary：没有变更值就说没有）', () => {
    expect(formatChange('', '')).toBe('')
    expect(formatChange(null, undefined)).toBe('')
    expect(formatChange('   ', '')).toBe('')
  })

  it('只有一侧有值 ⇒ 只显示那一侧（⛔ 不渲染「→ 95.00」这种半截箭头）', () => {
    // `order.create` 这类动作只写 `to_value`（发单价），带上悬空箭头的话**大多数行**都会
    // 以一个孤零零的箭头开头。
    expect(formatChange('', '95.00')).toBe('95.00')
    expect(formatChange('原值', '')).toBe('原值')
  })

  it('列的 `templet` 渲染的是这个值（且过了转义）', () => {
    const col = colByTitle(buildCols(), '变更内容')
    const host = renderCell(col, toLogRow(ROW))
    expect(host.textContent).toBe('代练中 → 待验收')
    const escaped = renderCell(col, toLogRow({ ...ROW, from_value: '<img src=x>', to_value: '' }))
    expect(escaped.innerHTML).not.toContain('<img')
    expect(escaped.textContent).toBe('<img src=x>')
  })
})

describe('C. 接口行 → 列数据（7 个列字段）', () => {
  it('11 个接口键正确落到 7 列上（含 order_no / source_order_no / action_label）', () => {
    expect(toLogRow(ROW)).toEqual({
      viewOrderId: '20261004000000000001',
      tid: 'TB-20261004000000000002',
      gameName: '测试游戏甲',
      title: '代练中 → 待验收',
      actionName: '更改状态',
      actionUserName: '测试昵称甲',
      actionTime: '2026-10-04 12:30:45',
    })
  })

  it('`actor_display` 缺席时回退 `actor`（不能变成空白）', () => {
    const legacy = { ...ROW } as Record<string, unknown>
    delete legacy.actor_display
    expect(toLogRow(legacy as OrderActionLogRow).actionUserName).toBe('kefu_jia')
  })

  it('统计行 → 右表行（操作 / **操作人显示账号名称** / 操作次数）', () => {
    // 「操作人」列显示 `actor_display`（昵称），⛔ 不是登录账号 `kefu_jia`（两者不同形，读错键立刻红）。
    expect(toSummaryRow(SUMMARY[0]!)).toEqual({
      actionName: '更改状态',
      actionUserName: '测试昵称甲',
      totalCount: 7,
    })
  })

  it('统计行缺 `actor_display`（旧后端）⇒ 回落 `actor` 账号，⛔ 不是空白', () => {
    const legacy = { ...SUMMARY[0]! } as Record<string, unknown>
    delete legacy.actor_display
    expect('actor_display' in legacy).toBe(false)
    expect(toSummaryRow(legacy as unknown as OrderActionLogSummaryRow).actionUserName).toBe(
      'kefu_jia',
    )
  })

  it('「系统单号」列是**可点**锚（class + 蓝色 + `data-order-no`）；「交易单号」列是蓝链、`title` 只写平台单号这一事实', () => {
    const orderCell = renderCell(colByTitle(buildCols(), '系统单号'), toLogRow(ROW))
    const a = orderCell.querySelector('a')!
    expect(a.className).toBe('orderdetails')
    expect(a.getAttribute('style')).toBe('color:#0094ff')
    // ⛔ 不抄对标的 `data-id`（那是它的**主键**，本页拿不到、也不该拿，spec §2.2/D12）。
    expect(a.hasAttribute('data-id')).toBe(false)
    expect(a.dataset.orderNo).toBe('20261004000000000001')
    expect(a.textContent).toBe('20261004000000000001')

    const tidCell = renderCell(colByTitle(buildCols(), '交易单号'), toLogRow(ROW))
    const b = tidCell.querySelector('a')!
    expect(b.className).toBe('viewPlatmTid')
    expect(b.getAttribute('style')).toBe('color:#1e9fff')
    expect(b.dataset.tid).toBe('TB-20261004000000000002')
    // ⚠️ `title` **只写事实**（spec §10.2）：⛔ 不许再有「点击就能打开交易详情界面」那半句
    // （点了只提示「尚未开放」，见下面那条点击用例）。评审实测「把 title 改得更糟」时
    // **41 条全绿（SURVIVED）** ⇒ 这一条断言就是补上的那个缺口。
    expect(b.getAttribute('title')).toBe('平台交易单号：TB-20261004000000000002')
  })

  it('单元格里的 HTML 被转义（单号/游戏名是请求来源的文本）', () => {
    const html = (colByTitle(buildCols(), '系统单号').templet as (d: LogRow) => string)(
      toLogRow({ ...ROW, order_no: '<img src=x onerror=alert(1)>' }),
    )
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })
})

describe('D. 排序事件 → 后端 ordering 词表（两向显式映射，认不出就不发）', () => {
  it('认的列就是「操作时间」列（`sort: true` 的那列）的 field', () => {
    const sortable = buildCols().filter((c) => c.sort === true)
    expect(sortable.map((c) => c.field)).toEqual([SORT_FIELD])
  })

  it('升序 → created_at；降序 → -created_at', () => {
    expect(toOrderActionLogOrdering('actionTime', 'asc')).toBe('created_at')
    expect(toOrderActionLogOrdering('actionTime', 'desc')).toBe('-created_at')
  })

  it('别的列 / 第三次点击的取消排序（type 为 null）/ 认不出的 type → undefined（不发参数）', () => {
    expect(toOrderActionLogOrdering('actionUserName', 'asc')).toBeUndefined()
    expect(toOrderActionLogOrdering('actionTime', null)).toBeUndefined()
    expect(toOrderActionLogOrdering('actionTime', undefined)).toBeUndefined()
    expect(toOrderActionLogOrdering(undefined, 'desc')).toBeUndefined()
  })
})

describe('E. 筛选栏 DOM 名 → 线上参数（唯一形状出口）', () => {
  it('五个控件的名字映射（sDate→stime / actionSelect→action / recUser→actor）', () => {
    expect(
      toOrderActionLogSearchParams({
        sDate: '2026-09-20',
        eDate: '2026-10-20',
        actionSelect: 'order.transition',
        recUser: 'kefu_jia',
        key1: '20261004000000000001',
      }),
    ).toEqual({
      stime: '2026-09-20',
      etime: '2026-10-20',
      action: 'order.transition',
      actor: 'kefu_jia',
      key1: '20261004000000000001',
    })
  })

  it('排序状态经第二个入口参数汇进参数对象；没传时是 undefined（不发参数）', () => {
    expect(toOrderActionLogSearchParams({}, 'created_at').ordering).toBe('created_at')
    expect(toOrderActionLogSearchParams({}, '-created_at').ordering).toBe('-created_at')
    expect(toOrderActionLogSearchParams({}).ordering).toBeUndefined()
  })

  it('「全部」（空串，选项由页面拼、候选来自后端的 `data.actions`）**不发** `action`', () => {
    // 后端 `action` 按 `ORDER_ACTION_LABELS` 的键做白名单：发一个空/非法值过去会当场 1000。
    const params = toOrderActionLogParams(
      toOrderActionLogSearchParams({ actionSelect: ACTION_ALL_VALUE }),
    )
    expect(params).not.toHaveProperty('action')
  })

  it('空值一律不发（线上形状 = 只有真筛选）；`ordering` 没点过排序时也不发', () => {
    const where = toOrderActionLogParams(
      toOrderActionLogSearchParams({
        sDate: '',
        eDate: '',
        actionSelect: '',
        recUser: '',
        key1: '',
      }),
    )
    expect(where).toEqual({})
    expect(
      toOrderActionLogParams(toOrderActionLogSearchParams({}, 'created_at')).ordering,
    ).toBe('created_at')
    expect(toOrderActionLogParams(toOrderActionLogSearchParams({}))).not.toHaveProperty('ordering')
  })
})

describe('F. 「操作日期」的默认区间（spec §3-D10）', () => {
  it('今天 − 1 个自然月 ~ 今天：快照日 2026-09-20 ⇒ 2026-08-20 / 2026-09-20（**31 天**）', () => {
    // 证据件 HTML L120/L124 的 `value` 就是这两个值。08-20→09-20 是 31 天（8 月有 31 天）
    // ⇒ **自然月**口径；⛔ 不是「30 天」、⛔ 不是 system-systemlog 的「昨天~今天」。
    expect(defaultActionLogDateRange(new Date(2026, 8, 20, 12, 34, 56))).toEqual({
      sDate: '2026-08-20',
      eDate: '2026-09-20',
    })
  })

  it('跨年也按自然月回退（2026-01-15 ⇒ 2025-12-15），且**不带时分秒**', () => {
    expect(defaultActionLogDateRange(new Date(2026, 0, 15, 0, 0, 0))).toEqual({
      sDate: '2025-12-15',
      eDate: '2026-01-15',
    })
  })
})

describe('G. 「操作动作」的候选**由后端给**（spec §3-D3 / §10.6：前端不镜像那张短名表）', () => {
  it('前端**没有**任何 code → 短名 的镜像：两个模块的**导出**（对象 / 数组 / `Map`）与**源码文本**都查不到（唯一出处是后端）', async () => {
    // ① **源码文本面**（spec §10.6 的「扫描面加上页面模块」）：形状无关——对象 / 数组 /
    //    `Map` / 别的构造都落在这一条上，**且不管那份镜像有没有导出**。
    //    两处各写同一批中文短名必有一处先漂移，而漂移的症状（下拉与表格的短名不一样）**没有网会报**。
    const sourceHits = MIRROR_SCAN_FILES.flatMap(({ file, label }) =>
      orderLiteralsIn(fs.readFileSync(file, 'utf8')).map((code) => `${label}: ${code}`),
    )
    expect(sourceHits).toEqual([])

    // ② **导出面**（运行时**值**）：源码文本藏得住的构造（如按前缀拼出来的键）在这里再兜一层。
    //    动态 import 是为了不引第二条同源 import 语句。
    const pageNs = (await import(
      '../order-tradingorderactionloglist'
    )) as unknown as Record<string, unknown>
    const apiNs = (await import('@/api/orderActionLogs')) as unknown as Record<string, unknown>
    expect(mirroredOrderCodes(pageNs, '页面模块')).toEqual([])
    expect(mirroredOrderCodes(apiNs, 'api 模块')).toEqual([])
  })
})

describe('H. 挂载与真交互（layui 换可控替身；本页已脱离 artifactPage）', () => {
  it('左表真正交给 layui 的那份：url/分页参数名/本地假排序关掉/toolbar/7 列', async () => {
    await mountPage()
    const opts = renderOptsFor(TABLE_ELEM)
    expect(opts.url).toBe(ORDER_ACTION_LOGS_URL)
    expect(ORDER_ACTION_LOGS_URL).toBe('/api/audit/order-action-logs/')
    expect(opts.method).toBe('get')
    expect(opts.request).toEqual({ pageName: 'page', limitName: 'page_size' })
    expect(opts.page).toEqual({ limit: 20, limits: [20, 30, 50] })
    expect(opts.toolbar).toBe(TOOLBAR_ELEM)
    expect(opts.defaultToolbar).toEqual(['filter'])
    // 关掉本地假排序（vendored layui 默认 true）——否则点箭头只重排当前页那 20 行。
    expect(opts.autoSort).toBe(false)
    // 页内没有 cellMinWidth / height（原 `artifactPage` 那份也没给）：⛔ 别顺手加上，
    // 那会改掉这一页在换渲染器时**不该变**的可见值。
    expect(opts.cellMinWidth).toBeUndefined()
    expect(opts.height).toBeUndefined()
    expect((opts.cols as ActionLogCol[][])[0]).toHaveLength(7)
  })

  it('首屏 where 就是筛选栏里显示的默认日期区间（不是全表最新 20 条）', async () => {
    const root = await mountPage()
    const sDate = root.querySelector<HTMLInputElement>('input#sDate')!.value
    const eDate = root.querySelector<HTMLInputElement>('input#eDate')!.value
    // 日期框是 laydate `type:'date'` ⇒ 值是**日期形状**（证据件的 `value="2026-08-20"` 同形）。
    expect(sDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(eDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(renderOptsFor(TABLE_ELEM).where).toEqual({ stime: sDate, etime: eDate })
    expect(LAYUI.laydate.render).toHaveBeenCalledWith({ elem: '#sDate', type: 'date', min: -90 })
    expect(LAYUI.laydate.render).toHaveBeenCalledWith({ elem: '#eDate', type: 'date' })
  })

  it('「操作动作」下拉：挂载时只有「全部」（对标那 10 个词一个不剩），候选随响应来', async () => {
    const root = await mountPage()
    const select = root.querySelector<HTMLSelectElement>('select#actionSelect')!
    const atMount = Array.from(select.options)
    // ⚠️ 响应回来之前**必须**先把对标的静态选项换掉：那 11 个是**它自己的**动作词表，
    // 与本页参数不对应 ⇒ 留着的话用户能选中一个「选了没结果」的选项。
    expect(atMount.map((o) => [o.value, o.textContent])).toEqual([['', '全部']])
    // 收集成清单再断言（`vitest/no-conditional-expect` 禁止在循环里 expect）。
    const labelsAtMount = atMount.map((o) => o.textContent ?? '')
    expect(BENCHMARK_ACTION_LABELS.filter((label) => labelsAtMount.includes(label))).toEqual([])
    // 换选项后要重渲下拉壳，否则页面上还是空壳。
    expect(LAYUI.form.render).toHaveBeenCalledWith('select')

    driveResponse()
    const filled = Array.from(select.options)
    // 「全部」由本页拼（`value` 空串），其余**原样**来自后端 `data.actions`（顺序都不动）。
    expect(filled.map((o) => o.value)).toEqual([
      ACTION_ALL_VALUE,
      ...ACTIONS.map((a) => a.code),
    ])
    expect(filled.map((o) => o.textContent)).toEqual(['全部', ...ACTIONS.map((a) => a.label)])
    expect(filled[0]!.selected).toBe(true)
  })

  it('「操作动作」候选**只填一次**（重填会把用户选中的动作重置回「全部」）', async () => {
    const root = await mountPage()
    driveResponse()
    const select = root.querySelector<HTMLSelectElement>('select#actionSelect')!
    // 用户选了第二项（`order.create`），随后一次翻页/刷新都会再响应一次 —— 选择必须保住。
    select.value = 'order.create'
    const firstFill = Array.from(select.options).map((o) => o.value)
    driveResponse()
    expect(Array.from(select.options).map((o) => o.value)).toEqual(firstFill)
    expect(select.value).toBe('order.create')
  })

  it('「操作人员」的壳按证据件的 `xm-select="recUser"` 渲染', async () => {
    await mountPage()
    // 证据件那一行还带 `xm-select-radio`（对标就是单选）——⛔ 别摘。
    expect(document.querySelector('select#recUser')!.hasAttribute('xm-select-radio')).toBe(true)
    const vendor = await import('@/layui/vendor')
    expect(vi.mocked(vendor.initFormSelects)).toHaveBeenCalledWith('#recUser')
  })

  it('点「搜索」→ `table.reload` 带上筛选栏的**线上参数**（回第 1 页）', async () => {
    const root = await mountPage()
    // 先让首屏响应回来（动作候选来自它），再改筛选、点搜索 —— 与真人操作同序。
    driveResponse()
    const sDate = root.querySelector<HTMLInputElement>('input#sDate')!.value
    const eDate = root.querySelector<HTMLInputElement>('input#eDate')!.value
    root.querySelector<HTMLInputElement>('input#key1')!.value = '20261004000000000009'
    ;(root.querySelector<HTMLSelectElement>('select#actionSelect')!).value = 'order.transition'
    LAYUI.formSelects.value.mockReturnValue('kefu_jia')

    const submit = LAYUI.formHandlers.get('submit(seachBtn)')
    expect(submit).toBeTypeOf('function')
    submit!()

    expect(LAYUI.table.reload).toHaveBeenCalledWith(TABLE_ID, {
      where: {
        stime: sDate,
        etime: eDate,
        action: 'order.transition',
        actor: 'kefu_jia',
        key1: '20261004000000000009',
      },
      page: { curr: 1 },
    })
  })

  it('「回车 = 搜索」：对 `#key1` 派发 Enter ⇒ `table.reload` 被调、参数现读、`page.curr === 1`（spec §10.1）', async () => {
    const root = await mountPage()
    driveResponse()
    const sDate = root.querySelector<HTMLInputElement>('input#sDate')!.value
    const eDate = root.querySelector<HTMLInputElement>('input#eDate')!.value
    const key1 = root.querySelector<HTMLInputElement>('input#key1')!
    key1.value = '20261004000000000009'
    const before = LAYUI.table.reload.mock.calls.length

    key1.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    )

    // 与「搜索」**同一条路**：多一次 reload、带上筛选栏现读的参数、`page` 回 1。
    expect(LAYUI.table.reload.mock.calls.length).toBe(before + 1)
    const [id, opts] = lastReload()
    expect(id).toBe(TABLE_ID)
    expect(opts.page).toEqual({ curr: 1 })
    expect(opts.where).toEqual({ stime: sDate, etime: eDate, key1: '20261004000000000009' })

    // 别的键不触发（回车是提交，方向键 / 字符键不是）。
    key1.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    expect(LAYUI.table.reload.mock.calls.length).toBe(before + 1)
  })

  it('⛔ 回车**不绑**两个日期输入框（laydate 自己把 Enter 当「确定」，再绑会抢同一个键）', async () => {
    const root = await mountPage()
    driveResponse()
    const before = LAYUI.table.reload.mock.calls.length
    // 只在循环里派发、断言在循环外（`vitest/no-conditional-expect`）。
    for (const selector of ['input#sDate', 'input#eDate']) {
      root
        .querySelector<HTMLInputElement>(selector)!
        .dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
        )
    }
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
  })

  it('「操作动作」选「全部」（空值）时请求里没有 `action`（键集合钉死）', async () => {
    const root = await mountPage()
    driveResponse()
    ;(root.querySelector<HTMLSelectElement>('select#actionSelect')!).value = ACTION_ALL_VALUE
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    const where = lastReload()[1].where
    expect(Object.keys(where).sort()).toEqual(['etime', 'stime'])
    expect(where).not.toHaveProperty('action')
  })

  it('选中一个真动作 ⇒ 参数里带 `action=<code>`（下发的是 code，不是中文短名）', async () => {
    const root = await mountPage()
    driveResponse()
    ;(root.querySelector<HTMLSelectElement>('select#actionSelect')!).value = 'order.create'
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    // 提交的是**候选里的 `code`**（后端 `action` 按 code 做白名单）；短名只用于显示。
    expect(lastReload()[1].where.action).toBe('order.create')
  })

  it('工具栏「刷新数据」用当前筛选重取（回第 1 页）；别的事件不重取', async () => {
    await mountPage()
    const toolbar = LAYUI.tableHandlers.get(`toolbar(${TABLE_FILTER})`)
    expect(toolbar).toBeTypeOf('function')
    toolbar!({ event: 'refresh' })
    expect(lastReload()[1].page).toEqual({ curr: 1 })
    const before = LAYUI.table.reload.mock.calls.length
    // 工具栏只有这一颗按钮（证据件 L176）——别的事件必须**什么都不做**。
    toolbar!({ event: 'something_else' })
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
  })

  it('点「操作时间」箭头 → 服务端 ordering；第三次点击（type null）回到默认序', async () => {
    await mountPage()
    const sort = LAYUI.tableHandlers.get(`sort(${TABLE_FILTER})`)
    expect(sort).toBeTypeOf('function')
    sort!({ field: 'actionTime', type: 'asc' })
    expect(lastReload()[1]).toMatchObject({ where: { ordering: 'created_at' }, page: { curr: 1 } })
    sort!({ field: 'actionTime', type: 'desc' })
    expect(lastReload()[1].where.ordering).toBe('-created_at')
    sort!({ field: 'actionTime', type: null })
    expect(lastReload()[1].where).not.toHaveProperty('ordering')
    // 排序不丢筛选：默认日期区间仍在。
    expect(lastReload()[1].where.stime).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('响应落到两张表：`parseData` 翻信封、`done` 用 `data.summary` 渲染右表（page:false、无工具区）', async () => {
    await mountPage()
    const parsed = driveResponse()
    expect(parsed.code).toBe(0)
    expect(parsed.count).toBe(41)
    expect(parsed.data).toEqual([toLogRow(ROW)])

    const t2 = renderOptsFor(TABLE2_ELEM)
    expect(t2.page).toBe(false)
    expect(t2.toolbar).toBeUndefined()
    expect(t2.defaultToolbar).toBeUndefined()
    expect((t2.cols as ActionLogCol[][])[0]).toHaveLength(3)
    expect(t2.data).toEqual([
      { actionName: '更改状态', actionUserName: '测试昵称甲', totalCount: 7 },
      { actionName: '录入订单', actionUserName: '测试昵称甲', totalCount: 3 },
    ])
  })

  it('「操作人员」候选来自 `data.actor_options`（**文本=昵称、值=账号**），且**只灌一次**（重灌会静默清掉用户的选择）', async () => {
    await mountPage()
    driveResponse()
    expect(LAYUI.formSelects.data).toHaveBeenCalledTimes(1)
    // ⚠️ `name` = `display`（昵称）、`value` = `actor`（登录账号）：两个方向都要钉住，
    // 只钉其中一个的话「两个都填昵称」或「两个都填账号」会各自漏过一半。
    expect(LAYUI.formSelects.data).toHaveBeenCalledWith('recUser', 'local', {
      arr: [
        { name: '测试昵称甲', value: 'kefu_jia' },
        { name: '测试昵称乙', value: 'kefu_yi' },
      ],
    })
    // 再响应一次（翻页/刷新都会走到）：⛔ 不许再灌——那会重置用户当前选的人。
    driveResponse()
    expect(LAYUI.formSelects.data).toHaveBeenCalledTimes(1)
  })

  it('旧后端只有 `data.actors`（没有 `actor_options`）⇒ 下拉按「文本=值=账号」兜底（部署窗口）', async () => {
    await mountPage()
    const opts = renderOptsFor(TABLE_ELEM)
    // 逐键构造旧形状（⛔ 不从 ENVELOPE.data 里 delete `actor_options`：那份是被别的用例共用的常量）。
    const legacyData = {
      items: ENVELOPE.data.items,
      total: ENVELOPE.data.total,
      summary: ENVELOPE.data.summary,
      actors: ENVELOPE.data.actors,
      actions: ENVELOPE.data.actions,
    }
    expect('actor_options' in legacyData).toBe(false)
    opts.parseData!({ code: 0, msg: '', data: legacyData })
    opts.done!()
    expect(LAYUI.formSelects.data).toHaveBeenCalledWith('recUser', 'local', {
      arr: [
        { name: 'kefu_jia', value: 'kefu_jia' },
        { name: 'kefu_yi', value: 'kefu_yi' },
      ],
    })
  })

  it('「操作人员」选中一项后请求里是 `actor=<登录账号>`（⛔ 不是下拉上显示的昵称）', async () => {
    await mountPage()
    driveResponse()
    // formSelects 的 `valStr` 回的就是候选项的 `value`（= 登录账号）⇒ 页面原样发给后端。
    LAYUI.formSelects.value.mockReturnValue('kefu_yi')
    LAYUI.formHandlers.get('submit(seachBtn)')!()
    expect(lastReload()[1].where.actor).toBe('kefu_yi')
    expect(lastReload()[1].where.actor).not.toBe('测试昵称乙')
  })

  it('「系统单号」用**列的 templet 真产物**点击：20 位才开层，开的是订单详情岛页', async () => {
    const root = await mountPage()
    const cell = renderCell(colByTitle(buildCols(), '系统单号'), toLogRow(ROW))
    const anchor = cell.querySelector('a')!
    root.appendChild(anchor)
    anchor.click()
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const opts = LAYUI.layer.open.mock.calls[0]![0] as Record<string, unknown>
    expect(opts.type).toBe(2)
    // ⛔ 不走 `ctx.onNavigate`：order-orderlist 等 4 页刻意从它改开层。
    expect(String(opts.content)).toContain(
      'layui-island/page.html?page=order-orderview&order_no=20261004000000000001',
    )
    expect(String(opts.title)).toContain('订单详情')
    expect(LAYUI.msgs).toEqual([])
  })

  it('「系统单号」形状闸：单号形状不对 ⇒ **不开层**、只给固定文案（⛔ 不把输入拼进提示）', async () => {
    const root = await mountPage()
    const bad = '<img src=x onerror=alert(1)>'
    const cell = renderCell(colByTitle(buildCols(), '系统单号'), toLogRow({ ...ROW, order_no: bad }))
    const anchor = cell.querySelector('a')!
    // `dataset` 会把 attribute 实体解码回 `<`/`>`——那正是「必须有形状闸」的理由：
    // `layer.open` 的 `title` 是 HTML 上下文，templet 里那次 escape 在这条链上不构成保护。
    expect(anchor.dataset.orderNo).toBe(bad)
    root.appendChild(anchor)
    anchor.click()
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msgs).toEqual(['订单号格式不正确，无法打开订单详情'])
    expect(LAYUI.msgs.join('')).not.toContain('<img')
  })

  it('「交易单号」照对标渲染成蓝链，但点了只提示「尚未开放」（不开层、不置灰）', async () => {
    const root = await mountPage()
    const cell = renderCell(colByTitle(buildCols(), '交易单号'), toLogRow(ROW))
    const anchor = cell.querySelector('a')!
    expect(anchor.getAttribute('style')).toBe('color:#1e9fff')
    root.appendChild(anchor)
    anchor.click()
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msgs).toEqual(['「交易详情」尚未开放'])
  })

  it('陷阱 ③：先 `bootstrapIsland`（此时标记还没塞）再塞标记，最后才 `initIslandElements`', async () => {
    await mountPage()
    expect(LAYUI.state.bootstrapSawMarkup).toBe(false)
    expect(LAYUI.state.initSawMarkup).toBe(true)
  })

  it('证据件的两张表标记与工具栏模板（按钮文案「刷新数据」逐字）都在页面上', async () => {
    const root = await mountPage()
    expect(root.querySelector('#tableList')).toBeTruthy()
    expect(root.querySelector('#tableList2')).toBeTruthy()
    // ⚠️ 工具栏是 `<script type="text/html" id="toolbarDemo">`（layui 的**模板**）：它里面的标记
    // **不进 DOM 树**，所以只能按模板原文断言——layui 渲染工具栏时读的正是这段字符串。
    const toolbarTpl = document.getElementById('toolbarDemo')!
    expect(toolbarTpl.textContent).toContain('lay-event="refresh"')
    // 文案逐字是「刷新数据」（证据件 L176）；⛔ 不是系统跟踪日志页的「刷新列表」。
    expect(toolbarTpl.textContent).toContain('刷新数据')
    expect(toolbarTpl.textContent).not.toContain('刷新列表')
  })
})
