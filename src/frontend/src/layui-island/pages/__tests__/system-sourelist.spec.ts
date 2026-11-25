// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OrderSourceRow } from '@/api/orderSources'

/**
 * 【订单来源管理】island 页（`pages/system-sourelist.ts`）的契约测试。
 *
 * 覆盖 spec §8 的前端 **#12–#17**：弹窗参数/初值/编辑预填（#12）、四条校验（#13）、
 * 9 个 radio（#14）、店铺两套文案（#15）、短信两列只提示不发请求（#16）、
 * 列表 7 列与四个 `<a>` templet + `page:false`/`defaultToolbar`（#17）；
 * 另加 `page_size=200` 的分页契约（spec §4 / 评审 Critical 1）。
 *
 * 手法照 `order-insiderecorderlist.spec.ts`：`vi.mock` 掉 `@/layui/island` 的
 * `bootstrapIsland`（jsdom 里加载不了真 layui）与 `@/api/orderSources`，只断言**入参**；
 * 证据件用**真实文件**（`public/replica/system-sourelist.html`），不另造一份标记——
 * 否则「弹窗里到底有没有那 9 个 radio / 三个 maxlength」这类断言会与真实产物脱钩。
 *
 * ⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 */
vi.mock('@/api/orderSources', () => ({
  fetchOrderSources: vi.fn(),
  createOrderSource: vi.fn(),
  updateOrderSource: vi.fn(),
  removeOrderSource: vi.fn(),
  // ⭐ 公司店铺分组：字典 + 三写函数（计划 §3.3 第 5/6 条）。
  fetchOrderSourceGroups: vi.fn(),
  createOrderSourceGroup: vi.fn(),
  updateOrderSourceGroup: vi.fn(),
  removeOrderSourceGroup: vi.fn(),
}))
vi.mock('@/api/xgjBindings', () => ({
  fetchXgjShopsList: vi.fn(),
}))

/**
 * layui 的**可控替身**。`form.val` 刻意实现成「按 `<form lay-filter>` 的作用域找输入」——
 * 这正是「先预填、再 open」那条硬要求的判据：一旦把顺序写反（先 open 把 `.change` 搬出
 * `<form>`，再 `form.val`），替身就找不到输入，编辑预填断言变红。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formVals: Array<Record<string, unknown>> = []
  const open = vi.fn<(config: Record<string, unknown>) => number>(() => 1)
  return {
    msg,
    tableHandlers,
    formHandlers,
    formVals,
    open,
    form: {
      render: vi.fn(),
      val: vi.fn((filter: string, data: Record<string, unknown>) => {
        formVals.push(data)
        const form = document.querySelector<HTMLFormElement>(`form[lay-filter="${filter}"]`)
        if (!form) return
        const fields = form.elements as unknown as Record<string, RadioNodeList | undefined>
        for (const [name, value] of Object.entries(data)) {
          const field = fields[name]
          if (!field) continue
          field.value = String(value)
        }
      }),
      on: (e: string, cb: (...args: unknown[]) => unknown) => formHandlers.set(e, cb),
    },
    table: {
      render: vi.fn(),
      reloadData: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: { msg, open, close: vi.fn(), confirm: vi.fn(), prompt: vi.fn(), tips: vi.fn() },
    element: { init: vi.fn() },
    /**
     * `layer.open({type:1, content})` 要的是 **jQuery 对象**（`config-replica:335`；传裸
     * `HTMLElement` 真 layui 会抛 `TypeError: r.parents is not a function`）——替身只包一层，
     * 断言用的 `content.el` 就是那个节点。
     */
    $: (el: Element) => ({ el }),
  }
})

vi.mock('@/layui/island', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui/island')>()
  return {
    ...actual,
    bootstrapIsland: (async () => LAYUI) as unknown as typeof actual.bootstrapIsland,
  }
})

import {
  createOrderSource,
  createOrderSourceGroup,
  fetchOrderSourceGroups,
  fetchOrderSources,
  removeOrderSource,
  removeOrderSourceGroup,
  updateOrderSource,
  updateOrderSourceGroup,
  type OrderSourceGroupRow,
} from '@/api/orderSources'
import { fetchXgjShopsList, type XgjShopsListRow } from '@/api/xgjBindings'
import { BizError } from '@/api/errors'
import {
  buildSourelistCols,
  COLS,
  fillGroupSelect,
  GROUP_SELECT_NAME,
  GROUP_TOOLBAR_EVENT,
  parseOrderBy,
  SHOPS_BLOCK_SELECTOR,
  SHOPS_EMPTY_TEXT,
  SHOPS_GROUP_SELECT_ID,
  SHOPS_SOURCE_SELECT_ID,
  SHOPS_TABLE_ID,
  SHOP_TYPE_RADIOS,
  shopTypeLabel,
  soureDialogOptions,
  TABLE_ID,
  validateSoureForm,
  mountSourelistPage,
  type SoureCellRow,
} from '../system-sourelist'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/system-sourelist.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/system-sourelist.html'),
  'utf8',
)

/** 一行真实形状的列表载荷（字段集 = `OrderSourceSerializer`，9 键；含分组两列）。 */
const ROW: OrderSourceRow = {
  id: 7,
  name: '抖音渠道',
  remark: '直播间来的单',
  order_by: 3,
  shop_type: 20,
  group: 4,
  group_name: '甲组',
  created_at: '2026-09-21T10:00:00+08:00',
  updated_at: '2026-09-21T10:00:00+08:00',
}

/** 表格行的真实形状（`toRow` 的产物；键名是对标拼写，`E-0024` 族：弹层预填按它读）。 */
const TABLE_ROW = {
  id: ROW.id,
  soureName: ROW.name,
  remark: ROW.remark,
  shopType: ROW.shop_type,
  orderBy: ROW.order_by,
  groupName: ROW.group_name,
  group: ROW.group,
} as unknown as OrderSourceRow

/** 一条分组字典行（`OrderSourceGroupRow`；`source_count` 是列表注解）。 */
const GROUP: OrderSourceGroupRow = {
  id: 4,
  name: '甲组',
  order_by: 0,
  source_count: 2,
  created_at: '2026-10-26T10:00:00+08:00',
  updated_at: '2026-10-26T10:00:00+08:00',
}

/** 「店铺归属」子表的一行（后端 `XgjShopRowSerializer`）。 */
const SHOP: XgjShopsListRow = {
  id: 11,
  shop_name: '甲店',
  user_name: '甲会员',
  order_source: 7,
  order_source_name: '抖音渠道',
  group_id: 4,
  group_name: '甲组',
  is_valid: true,
  valid_end_time: null,
  subscription_warning: false,
  binding: 2,
  binding_name: '应用甲',
}

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** 挂载整页：证据件走全局 fetch（真文件），列表走 `@/api/orderSources` 的替身。 */
async function mount(options: {
  rows?: OrderSourceRow[]
  groups?: OrderSourceGroupRow[]
} = {}): Promise<{
  root: HTMLElement
  cleanup: () => void
  cfg: Record<string, unknown>
}> {
  vi.mocked(fetchOrderSources).mockResolvedValue({
    items: options.rows ?? [ROW],
    total: (options.rows ?? [ROW]).length,
    page: 1,
    page_size: 200,
  })
  const groups = options.groups ?? [GROUP]
  vi.mocked(fetchOrderSourceGroups).mockResolvedValue({
    items: groups,
    total: groups.length,
    page: 1,
    page_size: 200,
  })
  vi.mocked(fetchXgjShopsList).mockResolvedValue({
    items: [SHOP],
    total: 1,
    page: 1,
    page_size: 200,
  })
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountSourelistPage(root, { onNavigate: vi.fn() })
  // ⚠️ 两张表都调了 `table.render`（主表 + 店铺子表）⇒ 按 id 取主表那份配置，
  //    ⛔ 不靠调用下标（先后顺序变了会静默取错表）。
  const mainCall = LAYUI.table.render.mock.calls.find(
    (call) => (call[0] as { id?: string }).id === TABLE_ID,
  )
  const cfg = mainCall![0] as Record<string, unknown>
  return { root, cleanup, cfg }
}

/** 取子表「店铺归属」的 `table.render` 配置。 */
function shopsTableConfig(): Record<string, unknown> {
  const call = LAYUI.table.render.mock.calls.find(
    (item) => (item[0] as { id?: string }).id === SHOPS_TABLE_ID,
  )
  expect(call, '店铺子表没有 table.render').toBeTruthy()
  return call![0] as Record<string, unknown>
}

/** 点一个真实元素（同步 `click` 事件；分组管理区块的监听器挂在节点自己身上）。 */
function dispatchClick(el: Element): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

/** 点一次工具栏事件（layui 的 `table.on('toolbar(tableList)')`）。 */
function fireToolbar(event: string): void {
  const cb = LAYUI.tableHandlers.get(`toolbar(${TABLE_ID})`)
  expect(cb, 'toolbar 处理器没登记').toBeTruthy()
  cb!({ event })
}

/** 点一次行内事件（layui 的 `table.on('tool(tableList)')`）。 */
function fireTool(event: string, row: OrderSourceRow = ROW): void {
  const cb = LAYUI.tableHandlers.get(`tool(${TABLE_ID})`)
  expect(cb, 'tool 处理器没登记').toBeTruthy()
  cb!({ event, data: row })
}

/** 当前弹层节点（`layer.open` 拿到的那个）。 */
function openedPanel(): HTMLElement {
  const call = LAYUI.open.mock.calls[LAYUI.open.mock.calls.length - 1]
  expect(call, 'layer.open 没被调用').toBeTruthy()
  return (call![0] as { content: { el: HTMLElement } }).content.el
}

/** 最近一次 `layer.open` 的整个配置。 */
function lastOpenConfig(): Record<string, unknown> {
  const call = LAYUI.open.mock.calls[LAYUI.open.mock.calls.length - 1]
  expect(call, 'layer.open 没被调用').toBeTruthy()
  return call![0] as Record<string, unknown>
}

/** 直接点页内某个元素（`bubbles: true`：短信两列靠 root 上的**捕获阶段**委托）。 */
function click(root: HTMLElement, el: Element): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  void root
}

/**
 * 清掉替身的调用记录。用途是**顺序判据**：`mock.invocationCallOrder` 是全局自增计数器，
 * 必须用 `clearAllMocks()` 清零，否则跨用例的绝对值不可比。
 */
function clearInvocations(): void {
  vi.clearAllMocks()
  LAYUI.formVals.length = 0
}

beforeEach(() => {
  LAYUI.table.render.mockClear()
  LAYUI.table.reloadData.mockClear()
  LAYUI.msg.mockClear()
  // ⚠️ 用 `mockReset`（不是 `mockClear`）：下面「先预填、再 open」那条会临时换掉
  //    `open` 的实现（模拟 layui 把节点搬进 `.layui-layer-content`），`mockClear` 只清调用记录、
  //    **保留实现** ⇒ 会把那个「搬走」行为泄漏给后面的用例（实测：重开用例因此假红）。
  LAYUI.open.mockReset()
  LAYUI.open.mockReturnValue(1)
  LAYUI.layer.close.mockClear()
  LAYUI.layer.confirm.mockClear()
  LAYUI.form.render.mockClear()
  LAYUI.form.val.mockClear()
  LAYUI.formVals.length = 0
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  vi.mocked(fetchOrderSources).mockReset()
  vi.mocked(createOrderSource).mockReset()
  vi.mocked(updateOrderSource).mockReset()
  vi.mocked(removeOrderSource).mockReset()
  vi.mocked(fetchOrderSourceGroups).mockReset()
  vi.mocked(createOrderSourceGroup).mockReset()
  vi.mocked(updateOrderSourceGroup).mockReset()
  vi.mocked(removeOrderSourceGroup).mockReset()
  vi.mocked(fetchXgjShopsList).mockReset()
  vi.mocked(createOrderSource).mockResolvedValue(ROW)
  vi.mocked(updateOrderSource).mockResolvedValue(ROW)
  vi.mocked(removeOrderSource).mockResolvedValue(null)
  // 默认给好字典/子表响应：不走 `mount()` 的用例（如「取数失败」那条）也不会拿到 undefined。
  vi.mocked(fetchOrderSourceGroups).mockResolvedValue({
    items: [GROUP],
    total: 1,
    page: 1,
    page_size: 200,
  })
  vi.mocked(fetchXgjShopsList).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 200,
  })
  vi.mocked(createOrderSourceGroup).mockResolvedValue(GROUP)
  vi.mocked(updateOrderSourceGroup).mockResolvedValue(GROUP)
  vi.mocked(removeOrderSourceGroup).mockResolvedValue(null)
  LAYUI.layer.prompt.mockReset()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('system-sourelist.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/* ───────────────────────── §2.3 两套文案 / 9 个 radio（#14 / #15） ───────────────────────── */

describe('哪种店铺：弹窗 9 个 radio 与列表两套文案', () => {
  it('SHOP_TYPE_RADIOS 恰好 9 项，value/title 逐字等于证据件（#14）', () => {
    expect(SHOP_TYPE_RADIOS).toEqual([
      { value: 0, title: '淘宝/天猫' },
      { value: 10, title: '拼多多' },
      { value: 15, title: '京东' },
      { value: 20, title: '闲鱼' },
      { value: 30, title: '抖音' },
      { value: 40, title: '快手' },
      { value: 50, title: '唯品会' },
      { value: 60, title: '小红书' },
      { value: 70, title: '其它平台' },
    ])
  })

  it('弹窗那份 9 个 radio 与**证据件原文**逐项一致（防两处漂移）', () => {
    const radios = Array.from(EVIDENCE.matchAll(/<input type="radio" name="shopType" value="(\d+)" title="([^"]+)">/g))
    expect(radios.map((m) => ({ value: Number(m[1]), title: m[2] }))).toEqual(
      SHOP_TYPE_RADIOS.map((r) => ({ ...r })),
    )
  })

  it('`shopTypeLabel(0)` = 非店铺/淘宝/天猫（与弹窗的 淘宝/天猫 **刻意不同**，不与 999 混同）', () => {
    expect(shopTypeLabel(0)).toBe('非店铺/淘宝/天猫')
    expect(shopTypeLabel(999)).toBe('非店铺/淘宝/天猫')
    expect(shopTypeLabel(20)).toBe('闲鱼')
    expect(shopTypeLabel(70)).toBe('其它平台')
  })
})

/* ───────────────────────── 排序数值容错（#13 的后两条） ───────────────────────── */

describe('parseOrderBy：只接受非负整数，其余静默按 0', () => {
  it.each([
    ['', 0],
    ['0', 0],
    ['12', 12],
    ['abc', 0],
    ['1.5', 0],
    [' 7 ', 7],
    ['1e3', 0],
    ['1_000', 0],
    ['-1', 0],
    ['999', 999],
  ])('parseOrderBy(%j) === %i', (raw, expected) => {
    expect(parseOrderBy(raw)).toBe(expected)
  })
})

/* ───────────────────────── 四条校验（#13） ───────────────────────── */

describe('validateSoureForm：四条（名称空 / 排序空 / 店铺未选→0 / 排序非整数→0）', () => {
  it('名称空 → 文案逐字「请输入来源名称」，其余归一成 0', () => {
    expect(validateSoureForm({ name: '', orderByRaw: '5', shopType: 20 })).toEqual({
      error: '请输入来源名称',
      orderBy: 0,
      shopType: 20,
    })
    expect(validateSoureForm({ name: '   ', orderByRaw: '5', shopType: 20 }).error).toBe(
      '请输入来源名称',
    )
  })

  it('排序空 → 文案逐字「请输入排序数值，可以写0」', () => {
    expect(validateSoureForm({ name: '闲鱼', orderByRaw: '', shopType: 20 })).toEqual({
      error: '请输入排序数值，可以写0',
      orderBy: 0,
      shopType: 20,
    })
  })

  it('店铺未选（null）→ **不报错**，归一成 0（淘宝/天猫）', () => {
    expect(validateSoureForm({ name: '闲鱼', orderByRaw: '3', shopType: null })).toEqual({
      error: '',
      orderBy: 3,
      shopType: 0,
    })
  })

  it('排序非整数 → **不报错**，归一成 0；整数照留（含 0）', () => {
    expect(validateSoureForm({ name: '闲鱼', orderByRaw: 'abc', shopType: 10 })).toEqual({
      error: '',
      orderBy: 0,
      shopType: 10,
    })
    expect(validateSoureForm({ name: '闲鱼', orderByRaw: '0', shopType: 10 })).toEqual({
      error: '',
      orderBy: 0,
      shopType: 10,
    })
    expect(validateSoureForm({ name: '闲鱼', orderByRaw: ' 12 ', shopType: 10 })).toEqual({
      error: '',
      orderBy: 12,
      shopType: 10,
    })
  })
})

/* ───────────────────────── 列骨架 + templet（#15 / #17） ───────────────────────── */

describe('buildSourelistCols：8 列骨架（含第 3 列「公司店铺分组」）+ 四个 `<a>` templet', () => {
  const cols = buildSourelistCols()
  const byField = (field: string): Record<string, unknown> =>
    cols.find((c) => c.field === field)!

  it('8 列：第 3 列是新增的「公司店铺分组」，其余 7 列逐项照 `sourelist.js` L21-87（操作列下移到 `cols[7]`）', () => {
    expect(cols).toHaveLength(8)
    expect(cols.map((c) => c.title)).toEqual([
      '来源名称',
      '来源说明',
      '公司店铺分组',
      '哪种店铺',
      '短信签名',
      '订单是否可发送短信<i class="layui-icon layui-bangzhu" style="color:#FF5722" title="正常(绿色)：代表该来源的订单会根据系统设置的短信业务来发送短信通知；禁用(灰色)：表示该来源的订单，将禁止发送任何短信通知" />',
      '排序(数值越大越排前)',
      '操作',
    ])
    expect(byField('soureName').fixed).toBe('left')
    expect(byField('soureName').width).toBe(130)
    expect(byField('soureName').align).toBe('left')
    expect(byField('remark').width).toBeUndefined()
    expect(byField('remark').align).toBe('left')
    // ⭐ 第 3 列（计划 §3.3 第 1 条）：插在「来源说明」之后 ⇒ 数组索引 2、左对齐、宽度留空。
    expect(cols[2]!.field).toBe('groupName')
    expect(cols[2]!.title).toBe('公司店铺分组')
    expect(cols[2]!.align).toBe('left')
    expect(cols[2]!.width).toBeUndefined()
    expect(byField('shopType').width).toBe(115)
    expect(byField('shopType').align).toBe('center')
    expect(byField('smsSign').width).toBe(200)
    expect(byField('smsSign').align).toBe('left')
    expect(byField('doNotSendSms').width).toBe(180)
    expect(byField('doNotSendSms').align).toBe('center')
    expect(byField('doNotSendSms').templet).toBe('#switchTpl1')
    expect(byField('orderBy').width).toBe(160)
    expect(byField('orderBy').align).toBe('center')
    expect(byField('操作')).toBeUndefined()
    expect(cols[7]!.title).toBe('操作')
    expect(cols[7]!.width).toBe(160)
    expect(cols[7]!.toolbar).toBe('#baroperating')
    expect(cols[7]!.field).toBeUndefined()
  })

  it('骨架与 `COLS` 是同一份出处（不会声明漂移）', () => {
    expect(cols.map((c) => c.field)).toEqual(COLS.map((c) => c.field))
    expect(cols.map((c) => c.title)).toEqual(COLS.map((c) => c.title))
    expect(cols.map((c) => c.width)).toEqual(COLS.map((c) => c.width))
  })

  it('来源名称列：`<a>` + **空 `class=""`** + `title` = 名称（对标原文，不是我们漏了）', () => {
    const cell = byField('soureName').templet as (r: SoureCellRow) => string
    expect(cell({ soureName: '闲鱼' })).toBe(
      '<a href="javascript:void(0);" class="" title="闲鱼">闲鱼</a>',
    )
  })

  it('来源说明列：同形 `<a>`，取 `remark`', () => {
    const cell = byField('remark').templet as (r: SoureCellRow) => string
    expect(cell({ remark: '描述' })).toBe(
      '<a href="javascript:void(0);" style="" title="描述">描述</a>',
    )
  })

  /**
   * **I-4（整体评审，安全）**：`templet` 的返回值被 layui **当 HTML 插入**，而 `soureName`
   * 是用户输入。后端的 `name` 上限是 **10 字**（`OrderSource.name`，`max_length=10`）⇒ 用
   * 一段**装得进 10 字**的 payload `"><img>`：它会在 `title="…"` 这个**属性上下文**里先闭合引号。
   */
  it('来源名称列：`name` 被转义（`title` 属性上下文不能被闭合）', () => {
    const cell = byField('soureName').templet as (r: SoureCellRow) => string
    const html = cell({ soureName: '"><img>' })
    expect(html).toBe(
      '<a href="javascript:void(0);" class="" title="&quot;&gt;&lt;img&gt;">&quot;&gt;&lt;img&gt;</a>',
    )
    expect(html).not.toContain('<img')
  })

  it('来源说明列：`remark` 整段 XSS payload 被转义（上限 50，装得下）', () => {
    const cell = byField('remark').templet as (r: SoureCellRow) => string
    const payload = '<img src=x onerror=alert(1)>'
    const html = cell({ remark: payload })
    expect(html).toBe(
      '<a href="javascript:void(0);" style="" title="&lt;img src=x onerror=alert(1)&gt;">' +
        '&lt;img src=x onerror=alert(1)&gt;</a>',
    )
    expect(html).not.toContain('<img')
  })

  it('公司店铺分组列：空串显示「未分组」；有名字时过 `escapeHtml`（属性/文本上下文都不能被闭合）', () => {
    const cell = byField('groupName').templet as (r: SoureCellRow) => string
    expect(cell({ groupName: '' })).toBe('未分组')
    expect(cell({ groupName: undefined })).toBe('未分组')
    expect(cell({ groupName: '甲组' })).toBe('甲组')
    expect(cell({ groupName: '<img src=x>' })).toBe('&lt;img src=x&gt;')
  })

  it('哪种店铺列走 `shopTypeLabel`（0 落 default ⇒ 非店铺/淘宝/天猫）', () => {
    const cell = byField('shopType').templet as (r: SoureCellRow) => string
    expect(cell({ shopType: 0 })).toBe('非店铺/淘宝/天猫')
    expect(cell({ shopType: 20 })).toBe('闲鱼')
    expect(cell({ shopType: undefined })).toBe('非店铺/淘宝/天猫')
  })

  it('排序列：`<a>` 包住数值，`title` 也是数值原文', () => {
    const cell = byField('orderBy').templet as (r: SoureCellRow) => string
    expect(cell({ orderBy: 3 })).toBe('<a href="javascript:void(0);" style="" title="3">3</a>')
    expect(cell({ orderBy: 0 })).toBe('<a href="javascript:void(0);" style="" title="0">0</a>')
  })

  it('短信签名列：`data-id` **照抄对标**取行 id（`res.id`）；`smsSign` 我们没有 ⇒ 文本/`data-orginsmssign`/`lay-tips` 为空', () => {
    const cell = byField('smsSign').templet as (r: SoureCellRow) => string
    const html = cell({ id: 7 })
    expect(html).toContain('class="modifySmsSign"')
    // ⚠️ 对标的 `data-id` 取的是 **行 id**（`sourelist.js:73`：`data-id="' + res.id + '"`），
    //    与 `smsSign` 无关。T5 第一版把这里写成恒空并把错值钉进断言 ⇒ 按 T5 评审 Important 修。
    //    期望值写**字面值 7**（E-0024：不拿导出常量自指）。
    expect(html).toContain('data-id="7"')
    expect(html).not.toContain('data-id=""')
    expect(html).toContain('data-orginsmssign=""')
    expect(html).toContain('href="javascript:void(0);"')
    expect(html).toContain('lay-tips=')
    expect(html).not.toContain('smsSign"')
    // id 缺失时退化成空串（同对标拼 `undefined` 的**结果形状**是空），不抛、不写 `undefined`。
    expect(cell({})).toContain('data-id=""')
  })

  it('列表**不引用** `smsSign` / `doNotSendSms` 两个不存在的字段（#16 的负向断言）', () => {
    // 判据打在**列定义的可序列化文本**上：骨架 + templet 一旦引用这两个键就会命中。
    const text = JSON.stringify(cols, (key, value) => (typeof value === 'function' ? String(value) : value))
    expect(text).not.toContain('res.smsSign')
    expect(text).not.toContain('res.doNotSendSms')
    // `doNotSendSms` 只以 `field`（= 对标的列名）与 `templet:'#switchTpl1'` 出现。
    expect(byField('doNotSendSms').templet).toBe('#switchTpl1')
  })
})

/* ───────────────────────── 表格声明（#17 + 分页契约） ───────────────────────── */

describe('挂载：表格声明走 house 口径（页面自己取数），首屏只发一次 GET', () => {
  it('table.render：**不带 `url`** / page:false / defaultToolbar / toolbar / height / cellMinWidth', async () => {
    const { cleanup, cfg } = await mount()
    expect(cfg.elem).toBe('#tableList')
    expect(cfg.id).toBe(TABLE_ID)
    // ⚠️ 数据由页面自己取（`config-replica.ts:81` 的 house 口径）⇒ 表格**不能**配 `url`：
    //    配了 layui 会自己发一次，而且 `table.reloadData` 会走 ajax 分支、忽略我们传的 `data`
    //    （读码 `public/layui/layui.js` 的 table `pullData`：`renderData ? … : o.url ? ajax : data`）。
    expect(cfg.url).toBeUndefined()
    // 先渲染一张空表（`config-replica.ts:116-122` 的「必须先 render 一次」同因）。
    expect(cfg.data).toEqual([])
    expect(cfg.page).toBe(false)
    expect(cfg.defaultToolbar).toEqual(['filter'])
    expect(cfg.toolbar).toBe('#toolbarDemo')
    expect(cfg.height).toBe('full')
    expect(cfg.cellMinWidth).toBe(100)
    expect(cfg.text).toEqual({ none: '暂无数据' })
    expect((cfg.cols as unknown[][])[0]).toHaveLength(8)
    cleanup()
  })

  it('表格不带 `url`（layui 不会自己发请求）；来源表与「店铺归属」区块各自取一次数', async () => {
    const { cleanup, cfg } = await mount()
    // 判据一：表格没有 `url` ⇒ layui 不会自己发请求（也不能在 reloadData 时再发一次）。
    expect(cfg.url).toBeUndefined()
    // 判据二：`fetchOrderSources` 恰好两次 —— 来源表一次 + 「店铺归属」的来源下拉一次
    // （计划 §3.4：区块**自己取数**、与来源表不复用行数据）。⛔ 第三次就是重复请求。
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(2))
    cleanup()
  })

  it('首屏取数走 `fetchOrderSources`（拿**整页**对象），21 条一行不少地交给 `table.reloadData`', async () => {
    const rows = Array.from({ length: 21 }, (_, i) => ({ ...ROW, id: i + 1, name: `来源${i + 1}` }))
    const { cleanup } = await mount({ rows })
    // 2 次 = 来源表 + 「店铺归属」区块的来源下拉（计划 §3.4：区块自己取数）。
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(2))
    const reload = LAYUI.table.reloadData.mock.calls[0]!
    expect(reload[0]).toBe(TABLE_ID)
    const payload = reload[1] as { data: SoureCellRow[] }
    expect(payload.data).toHaveLength(21)
    // 行映射（`toRow`）用**字面值**钉住首尾两行（E-0024：不只数长度）。
    expect(payload.data[0]).toEqual({
      id: 1,
      soureName: '来源1',
      remark: '直播间来的单',
      shopType: 20,
      orderBy: 3,
      groupName: '甲组',
      group: 4,
    })
    expect(payload.data[20]).toEqual({
      id: 21,
      soureName: '来源21',
      remark: '直播间来的单',
      shopType: 20,
      orderBy: 3,
      groupName: '甲组',
      group: 4,
    })
    cleanup()
  })

  it('`page_size=200` 的分页契约由**唯一出口** `fetchOrderSources` 承担（首屏与 reload 都走它）', async () => {
    // 页面不再自带 `where`（表格没有 `url`，layui 不会用它拼任何请求 ⇒ 那是死配置）。
    // 这条把「首屏与 reload 用的是**同一个**取数出口」钉住：漏带 `page_size=200` 的那份实现
    // 在 `api/__tests__/orderSources.spec.ts:35-52` 会红（那里钉 `params: {page:1, page_size:200}`）。
    const { cleanup } = await mount()
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(2))
    // 刷新按钮走的是**同一个**出口（第 3 次调用）。
    fireToolbar('refresh')
    // 首屏 2 次（来源表 + 区块下拉）+ 刷新 1 次 = 3。
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(3))
    cleanup()
  })

  it('取数失败 → `msg(e.message)`，不抛、不留半张表', async () => {
    vi.mocked(fetchOrderSources).mockRejectedValue(new BizError(1002, '分页参数非法'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountSourelistPage(root, { onNavigate: vi.fn() })
    await Promise.resolve()
    await Promise.resolve()
    expect(LAYUI.msg).toHaveBeenCalledWith('分页参数非法', { time: 1500 })
    // 主表没有 reloadData（店铺子表那次是区块自己的、与来源表的取数失败无关）。
    expect(
      LAYUI.table.reloadData.mock.calls.some((call) => call[0] === TABLE_ID),
    ).toBe(false)
    cleanup()
  })
})

/* ───────────────────────── 弹窗（#12） ───────────────────────── */

describe('弹窗：分组下拉的降级护栏（2026-10-27 评审 M1）', () => {
  it('本行分组不在候选里时，补一条 option（否则保存会把它清成分组)）', () => {
    const select = document.createElement('select')
    // 分组字典取失败（`[]`）或该分组被 >200 截断 ⇒ 候选里没有本行的分组
    fillGroupSelect(select, [], { id: 4, name: '甲组' })
    expect([...select.options].map((o) => o.value)).toEqual(['', '4'])
    expect(select.options[1]?.textContent).toBe('甲组')
    // 空名字时给一个人话占位，不至于显示成空 option
    const select2 = document.createElement('select')
    fillGroupSelect(select2, [], { id: 7, name: '' })
    expect(select2.options[1]?.textContent).toBe('（当前分组）')
  })

  it('本行分组已在候选里时不重复追加', () => {
    const select = document.createElement('select')
    fillGroupSelect(select, [{ id: 4, name: '甲组' } as never], { id: 4, name: '甲组' })
    expect([...select.options].map((o) => o.value)).toEqual(['', '4'])
  })
})

describe('弹窗：layer.open 参数 + 初值 + 编辑预填', () => {
  it('`soureDialogOptions` 逐项照 `sourelist.js` L168-177（桌面分支的 area）', () => {
    expect(soureDialogOptions(false, 'CONTENT')).toEqual({
      title: '新增一个来源',
      shade: [0.3, '#333'],
      shadeClose: true,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['820px', '470px'],
      content: 'CONTENT',
    })
    expect(soureDialogOptions(true, 'CONTENT').title).toBe('编辑来源')
  })

  it('「新增一个订单来源」→ 三输入为空、排序 `0`、店铺 `0`、隐藏 id `0`，标题「新增一个来源」', async () => {
    const { root, cleanup } = await mount()
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const config = lastOpenConfig()
    expect(config.title).toBe('新增一个来源')
    expect(config.type).toBe(1)
    expect(config.shade).toEqual([0.3, '#333'])
    expect(config.shadeClose).toBe(true)
    expect(config.skin).toBe('layui-layer-lan')
    expect(config.anim).toBe(8)
    expect(config.area).toEqual(['820px', '470px'])
    expect(config.content).toBeTruthy()
    expect(LAYUI.formVals[0]).toEqual({
      soureName: '',
      soureRemark: '',
      soureOrderBy: '0',
      soureId: 0,
      shopType: 0,
      // ⭐ 分组首项 = 未分组（空串 ⇒ 保存时送 `group: null`，见保存那组用例）。
      soureGroup: '',
    })
    // 店铺初值 0 落地成**选中的 radio**（0 是合法值，不是「假值」——E-0021）。
    const panel = openedPanel()
    const checked = panel.querySelector<HTMLInputElement>('[name="shopType"]:checked')
    expect(checked?.value).toBe('0')
    expect(checked?.getAttribute('title')).toBe('淘宝/天猫')
    cleanup()
    void root
  })

  it('行内「修改」→ 编辑预填（名称/描述/排序/id/店铺），标题「编辑来源」', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', TABLE_ROW)
    expect(lastOpenConfig().title).toBe('编辑来源')
    expect(LAYUI.formVals[0]).toEqual({
      soureName: '抖音渠道',
      soureRemark: '直播间来的单',
      soureOrderBy: '3',
      soureId: 7,
      shopType: 20,
      soureGroup: '4',
    })
    expect(openedPanel().querySelector<HTMLInputElement>('[name="shopType"]:checked')?.value).toBe(
      '20',
    )
    // 分组下拉的候选来自分组字典，且预填成这一行的分组 id（4）。
    const groupSelect = openedPanel().querySelector<HTMLSelectElement>(
      `select[name="${GROUP_SELECT_NAME}"]`,
    )
    expect(groupSelect, '弹层里没有分组下拉（injectGroupSelect 没生效）').not.toBeNull()
    expect([...groupSelect!.options].map((o) => o.value)).toEqual(['', '4'])
    expect(groupSelect!.value).toBe('4')
    cleanup()
  })

  it('**先预填、再 open**：`layer.open` 在 `form.val` 与 `form.render` 之后，且拿到的是**仍在 `<form>` 里**的节点', async () => {
    const { root, cleanup } = await mount()
    clearInvocations()
    // 真 layui 的 `layer.open({type:1, content: jq})` 会把节点**搬出 `<form>`**；替身照此实现，
    // 于是「顺序写反」时真 `form.val` 的作用域会找不到输入（编辑打开是空表单）。
    LAYUI.open.mockImplementation(() => {
      const panel = document.querySelector<HTMLElement>('.change')
      const holder = document.createElement('div')
      holder.className = 'layui-layer-content'
      document.body.appendChild(holder)
      if (panel) holder.appendChild(panel)
      return 1
    })
    fireTool('submitEdit', TABLE_ROW)
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    // ⚠️ 这条是**顺序判据**：钉住 `layer.open` 是这三步里的**最后一步**。
    //    替身的 `form.val` 只记入参，所以「先 open 再预填」它**照样绿**（实测：MUT-C 全绿不改色）
    //    ——必须用 `invocationCallOrder` 直接钉顺序，否则这条断言是空的。
    expect(LAYUI.form.val.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.open.mock.invocationCallOrder[0]!,
    )
    expect(LAYUI.form.render.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.open.mock.invocationCallOrder[0]!,
    )
    expect(LAYUI.form.val).toHaveBeenCalledWith(
      'form',
      expect.objectContaining({ soureName: '抖音渠道' }),
    )
    expect(root.querySelector('form')!.contains(openedPanel())).toBe(false)
    cleanup()
  })

  it('关闭后节点被摘掉（`table.reload` 会重建那一带）→ 第二次打开仍能取到干净节点并预填', async () => {
    const { root, cleanup } = await mount()
    fireTool('submitEdit', TABLE_ROW)
    expect(LAYUI.formVals[0]!.soureName).toBe('抖音渠道')
    // `table.reloadData` 之后 layui 会重填工具栏：把节点整个从文档里摘掉（模拟重建）。
    const stolen = openedPanel()
    stolen.remove()
    LAYUI.open.mockClear()
    LAYUI.formVals.length = 0
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(LAYUI.formVals[0]).toEqual({
      soureName: '',
      soureRemark: '',
      soureOrderBy: '0',
      soureId: 0,
      shopType: 0,
      soureGroup: '',
    })
    const reopened = openedPanel()
    expect(reopened).not.toBe(stolen)
    expect(root.querySelector('form')!.contains(reopened)).toBe(true)
    cleanup()
  })
})

/* ───────────────────────── 保存 / 删除 ───────────────────────── */

describe('保存与删除：真发请求、失败原样显示、成功刷新', () => {
  it('新增：POST 载荷 = `{name, remark, order_by, shop_type}`，`order_by: 0` 照发', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    panel.querySelector<HTMLInputElement>('[name="soureName"]')!.value = '淘宝渠道'
    panel.querySelector<HTMLInputElement>('[name="soureRemark"]')!.value = '备注'
    panel.querySelector<HTMLInputElement>('[name="soureOrderBy"]')!.value = '0'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(createOrderSource).toHaveBeenCalledTimes(1))
    expect(createOrderSource).toHaveBeenCalledWith({
      name: '淘宝渠道',
      remark: '备注',
      order_by: 0,
      shop_type: 0,
      // 没选分组 ⇒ **`null`**（改成未分组/未分组新建；⛔ 不是 `0`、也不是漏这个键）。
      group: null,
    })
    cleanup()
  })

  it('新增：选了分组 ⇒ 载荷送分组 id（数字），不是字符串', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    panel.querySelector<HTMLInputElement>('[name="soureName"]')!.value = '淘宝渠道'
    // 下拉候选来自分组字典（openForm 里重建），选中「甲组」(id 4)。
    const groupSelect = panel.querySelector<HTMLSelectElement>(
      `select[name="${GROUP_SELECT_NAME}"]`,
    )!
    groupSelect.value = '4'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(createOrderSource).toHaveBeenCalledTimes(1))
    expect((vi.mocked(createOrderSource).mock.calls[0]![0] as { group?: unknown }).group).toBe(4)
    cleanup()
  })

  it('编辑：走 PATCH 且 id 取当前行；成功后关弹窗 + 刷新 + 「保存成功」', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', TABLE_ROW)
    const panel = openedPanel()
    panel.querySelector<HTMLInputElement>('[name="soureName"]')!.value = '抖音渠道'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(updateOrderSource).toHaveBeenCalledTimes(1))
    expect(updateOrderSource).toHaveBeenCalledWith(7, {
      name: '抖音渠道',
      remark: '直播间来的单',
      order_by: 3,
      shop_type: 20,
      // 这一行原本就在「甲组」⇒ 原样送回分组 id。
      group: 4,
    })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    // 首屏 2 次（来源表 + 区块下拉）+ 保存后刷新 1 次 = 3。
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(3))
    expect(LAYUI.msg).toHaveBeenCalledWith('保存成功', { time: 1500 })
    cleanup()
  })

  it('校验不过 → 只提示、**不发请求**', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await Promise.resolve()
    expect(LAYUI.msg).toHaveBeenCalledWith('请输入来源名称', { time: 1500 })
    expect(createOrderSource).not.toHaveBeenCalled()
    expect(updateOrderSource).not.toHaveBeenCalled()
    cleanup()
  })

  it('后端拒绝 → 原样显示 `e.message`（业务码 1001 走同一出口），不发「保存成功」', async () => {
    vi.mocked(createOrderSource).mockRejectedValue(new BizError(1001, '来源名称已存在'))
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    panel.querySelector<HTMLInputElement>('[name="soureName"]')!.value = '重名'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('来源名称已存在', { time: 1500 }))
    expect(LAYUI.msg).not.toHaveBeenCalledWith('保存成功', { time: 1500 })
    cleanup()
  })

  it('删除：`layer.confirm` → DELETE(id) → 刷新；确认语照实提示', async () => {
    const { cleanup } = await mount()
    fireTool('submitDel', ROW)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    const [text, cb] = LAYUI.layer.confirm.mock.calls[0] as unknown as [string, (i: number) => void]
    expect(typeof text).toBe('string')
    expect(text.length).toBeGreaterThan(0)
    cb(5)
    await vi.waitFor(() => expect(removeOrderSource).toHaveBeenCalledWith(7))
    expect(LAYUI.layer.close).toHaveBeenCalledWith(5)
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(3))
    cleanup()
  })

  it('刷新按钮 → 只是再取一次数（不重排、不弹窗）', async () => {
    const { cleanup } = await mount()
    LAYUI.open.mockClear()
    fireToolbar('refresh')
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(3))
    expect(LAYUI.open).not.toHaveBeenCalled()
    cleanup()
  })
})

/* ───────────────────────── 短信两列（#16） ───────────────────────── */

describe('短信两列：捕获阶段拦截 → 只提示、不发请求、状态不变', () => {
  /** 造一棵与行内渲染同形的树（开关的 class 与 `lay-filter` 照证据件 L98-100）。 */
  function smsTree(root: HTMLElement): { sign: HTMLAnchorElement; toggle: HTMLElement; outer: HTMLElement } {
    outer.innerHTML = ''
    outer.appendChild(root)
    const td = document.createElement('td')
    td.innerHTML =
      '<a class="modifySmsSign" data-id="" data-orginsmssign="" href="javascript:void(0);" style=""></a>' +
      '<input type="checkbox" name="doNotSendSms" lay-skin="switch" lay-text="可以发|已禁止发" lay-filter="doNotSendSms">' +
      '<div class="layui-form-switch"><em>已禁止发</em><i></i></div>'
    root.appendChild(td)
    return {
      sign: td.querySelector<HTMLAnchorElement>('.modifySmsSign')!,
      toggle: td.querySelector<HTMLElement>('.layui-form-switch')!,
      outer,
    }
  }
  let outer: HTMLElement
  beforeEach(() => {
    outer = document.createElement('div')
    document.body.appendChild(outer)
  })

  it('点短信签名 → 提示「「短信签名」尚未开放」、零请求、`checked` 不变', async () => {
    const { root, cleanup } = await mount()
    const { sign } = smsTree(root)
    const checkbox = root.querySelector<HTMLInputElement>('[name="doNotSendSms"]')!
    const before = checkbox.checked
    click(root, sign)
    expect(LAYUI.msg).toHaveBeenCalledWith('「短信签名」尚未开放', { time: 1500 })
    // 2 次 = 来源表 + 「店铺归属」区块的来源下拉；两个 SMS 用例都只关心「没有额外请求」。
    expect(fetchOrderSources).toHaveBeenCalledTimes(2)
    expect(createOrderSource).not.toHaveBeenCalled()
    expect(updateOrderSource).not.toHaveBeenCalled()
    expect(removeOrderSource).not.toHaveBeenCalled()
    expect(checkbox.checked).toBe(before)
    cleanup()
  })

  it('**捕获阶段 + `stopPropagation`**：点开关只提示，事件到不了祖先（也就到不了 layui 的真切换）', async () => {
    const { root, cleanup } = await mount()
    const { toggle } = smsTree(root)
    const checkbox = root.querySelector<HTMLInputElement>('[name="doNotSendSms"]')!
    expect(checkbox.checked).toBe(false)
    // `layui` 的 form 模块在**冒泡**阶段给 `.layui-form-switch` 挂处理：祖先收到事件就说明
    // 我们的 `stopPropagation` 没拦住 ⇒ 开关会被真切换（假成功）。
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    click(root, toggle)
    expect(LAYUI.msg).toHaveBeenCalledWith('「订单是否可发送短信」尚未开放', { time: 1500 })
    expect(leaked).not.toHaveBeenCalled()
    expect(checkbox.checked).toBe(false)
    expect(fetchOrderSources).toHaveBeenCalledTimes(2)
    outer.removeEventListener('click', leaked)
    cleanup()
  })

  it('`lay-filter="doNotSendSms"` 的开关本体（未包 switch 壳）同样被拦：事件到不了祖先', async () => {
    const { root, cleanup } = await mount()
    // ⚠️ **必须先经 `smsTree(root)` 把 root 挂进 `outer`**：T5 第一版直接 `root.appendChild(input)`，
    //    那时 `root` 挂在 `document.body` 上、**不在 `outer` 里** ⇒ 事件冒泡永远到不了 `outer`，
    //    于是 `expect(leaked).not.toHaveBeenCalled()` 是**恒真断言**（T5 评审 Minor ②：
    //    删掉 `stopPropagation` 的 MUT-3 下这条仍绿 = 判据为零）。
    //    经 `smsTree` 之后 root → outer 是一条**真实**的事件路径，这条断言才有判别力。
    smsTree(root)
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.setAttribute('lay-filter', 'doNotSendSms')
    root.appendChild(input)
    // ⚠️ 这里**不**断言 `input.checked`：jsdom 对**派发**的 click 不执行 checkbox 的默认激活行为，
    //    所以 `preventDefault` 拦不出 `checked` 的差别（实测：连一个最小探针里
    //    `preventDefault` 也无法让 `checked` 保持 false）。真 layui 那层是会拦的——
    //    判据改钉「事件被收口」这个**出口**（同 `artifactPage` 用捕获的理由）。
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    click(root, input)
    expect(LAYUI.msg).toHaveBeenCalledWith('「订单是否可发送短信」尚未开放', { time: 1500 })
    expect(leaked).not.toHaveBeenCalled()
    outer.removeEventListener('click', leaked)
    cleanup()
  })
})

/* ─────────────── 分组管理（2026-10-26，计划 §3.3 第 5 条） ─────────────── */

describe('分组管理：工具栏按钮 → layer 内增 / 改名 / 删', () => {
  /** 打开分组管理弹层并返回它的节点（默认 `layer.open` 不搬节点 ⇒ 节点仍在 root 里）。 */
  async function openManager(): Promise<{ root: HTMLElement; cleanup: () => void; manager: HTMLElement }> {
    const { root, cleanup } = await mount()
    fireToolbar(GROUP_TOOLBAR_EVENT)
    const manager = root.querySelector<HTMLElement>('.sourceGroupManager')
    expect(manager, '分组管理节点没建出来').not.toBeNull()
    return { root, cleanup, manager: manager! }
  }

  it('按钮真的注入了 `#toolbarDemo` 模板（不是往渲染结果里塞）；点它弹「分组管理」，列表带分组名与来源数', async () => {
    const { root, cleanup } = await mount()
    const template = root.querySelector<HTMLScriptElement>('#toolbarDemo')!
    expect(template.textContent).toContain(`lay-event="${GROUP_TOOLBAR_EVENT}"`)
    expect(template.textContent).toContain('分组管理')

    fireToolbar(GROUP_TOOLBAR_EVENT)
    expect(lastOpenConfig().title).toBe('分组管理')
    const manager = root.querySelector<HTMLElement>('.sourceGroupManager')!
    expect(manager.innerHTML).toContain('甲组')
    expect(manager.innerHTML).toContain('2 个来源')
    // 行上的两颗按钮各带自己的动作（处理器按 `data-group-action` 分派）。
    const actions = [...manager.querySelectorAll<HTMLElement>('[data-group-action]')].map(
      (el) => el.dataset.groupAction,
    )
    expect(actions).toEqual(['rename', 'delete'])
    cleanup()
  })

  it('新增分组 → `createOrderSourceGroup({name, order_by})`，随后重取分组字典与来源表', async () => {
    const { root, cleanup } = await openManager()
    root.querySelector<HTMLInputElement>('#groupNameInput')!.value = '乙组'
    root.querySelector<HTMLInputElement>('#groupOrderByInput')!.value = '5'
    dispatchClick(root.querySelector('#groupAddBtn')!)
    await vi.waitFor(() =>
      expect(createOrderSourceGroup).toHaveBeenCalledWith({ name: '乙组', order_by: 5 }),
    )
    // 增删改后必须重取：分组字典（第 2 次）+ 来源表（首屏 2 次 + 这次 = 3 次）。
    await vi.waitFor(() => expect(fetchOrderSourceGroups).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(fetchOrderSources).toHaveBeenCalledTimes(3))
    cleanup()
  })

  it('新增分组：名称为空 ⇒ 只提示「请输入分组名称」、不发请求', async () => {
    const { root, cleanup } = await openManager()
    dispatchClick(root.querySelector('#groupAddBtn')!)
    await Promise.resolve()
    expect(LAYUI.msg).toHaveBeenCalledWith('请输入分组名称', { time: 1500 })
    expect(createOrderSourceGroup).not.toHaveBeenCalled()
    cleanup()
  })

  it('改名 → `layer.prompt` 拿新名 → `updateOrderSourceGroup(id, {name})`', async () => {
    const { root, cleanup } = await openManager()
    LAYUI.layer.prompt.mockImplementation(((_o: unknown, cb: (v: string, i: number) => void) => {
      cb('乙组', 9)
    }) as never)
    dispatchClick(root.querySelector('[data-group-action="rename"]')!)
    await vi.waitFor(() =>
      expect(updateOrderSourceGroup).toHaveBeenCalledWith(4, { name: '乙组' }),
    )
    expect(LAYUI.layer.close).toHaveBeenCalledWith(9)
    cleanup()
  })

  it('删除：确认文案里的「N 个来源」**来自 `source_count`**（2 ⇒ 说 2）；确认后 DELETE', async () => {
    const { root, cleanup } = await openManager()
    dispatchClick(root.querySelector('[data-group-action="delete"]')!)
    const [text, cb] = LAYUI.layer.confirm.mock.calls[0] as unknown as [
      string,
      (index: number) => void,
    ]
    expect(text).toContain('甲组')
    expect(text).toContain('2 个来源将变成未分组')
    cb(3)
    await vi.waitFor(() => expect(removeOrderSourceGroup).toHaveBeenCalledWith(4))
    cleanup()
  })

  it('`source_count` 拿不到 ⇒ 文案里**不出现**「N 个来源」（⛔ 不写死 N）', async () => {
    const noCount: OrderSourceGroupRow = { ...GROUP, source_count: undefined }
    const { root, cleanup } = await mount({ groups: [noCount] })
    fireToolbar(GROUP_TOOLBAR_EVENT)
    dispatchClick(root.querySelector('[data-group-action="delete"]')!)
    const [text] = LAYUI.layer.confirm.mock.calls[0] as unknown as [string]
    expect(text).toContain('确定删除分组「甲组」吗？')
    expect(text).not.toContain('个来源')
    cleanup()
  })
})

/* ─────────────── 店铺归属区块（2026-10-26，计划 §3.4） ─────────────── */

describe('店铺归属区块：同页独立区块（不是 Tab）+ 两个筛选 + 子表', () => {
  it('区块存在、**没有页签标记**；子表 page:false、空态文案取自建页常量', async () => {
    const { root, cleanup } = await mount()
    expect(root.querySelector(SHOPS_BLOCK_SELECTOR), '店铺归属区块没插进来').not.toBeNull()
    // ⛔ 计划 §3.4 写死「同页独立区块，不做 Tab」：本页不该出现任何页签标记。
    expect(root.querySelector('.layui-tab')).toBeNull()
    const cfg = shopsTableConfig()
    expect(cfg.elem).toBe('#xgjShopsTable')
    expect(cfg.page).toBe(false)
    expect((cfg.text as { none?: string }).none).toBe(SHOPS_EMPTY_TEXT)
    cleanup()
  })

  it('首屏：两个下拉的候选来自字典（全部来源 / 全部分组 + 各自选项），子表只取一次数', async () => {
    const { root, cleanup } = await mount()
    const options = (id: string): string[] =>
      [...root.querySelectorAll<HTMLOptionElement>(`#${id} option`)].map((o) => o.value)
    await vi.waitFor(() => expect(fetchXgjShopsList).toHaveBeenCalledTimes(1))
    expect(fetchXgjShopsList).toHaveBeenCalledWith({})
    expect(options(SHOPS_SOURCE_SELECT_ID)).toEqual(['', '7'])
    expect(options(SHOPS_GROUP_SELECT_ID)).toEqual(['', '4'])
    cleanup()
  })

  it('两个筛选都进请求：改选即查；清空某一项 ⇒ **它的键消失**（不发空串）', async () => {
    const { root, cleanup } = await mount()
    await vi.waitFor(() => expect(fetchXgjShopsList).toHaveBeenCalledTimes(1))
    const source = root.querySelector<HTMLSelectElement>(`#${SHOPS_SOURCE_SELECT_ID}`)!
    const group = root.querySelector<HTMLSelectElement>(`#${SHOPS_GROUP_SELECT_ID}`)!

    source.value = '7'
    source.dispatchEvent(new Event('change'))
    await vi.waitFor(() => expect(fetchXgjShopsList).toHaveBeenLastCalledWith({ order_source: 7 }))

    group.value = '4'
    group.dispatchEvent(new Event('change'))
    await vi.waitFor(() =>
      expect(fetchXgjShopsList).toHaveBeenLastCalledWith({ order_source: 7, source_group: 4 }),
    )

    source.value = ''
    source.dispatchEvent(new Event('change'))
    await vi.waitFor(() =>
      expect(fetchXgjShopsList).toHaveBeenLastCalledWith({ source_group: 4 }),
    )
    cleanup()
  })

  it('子表把店铺行灌进 `reloadData`（含分组名）', async () => {
    const { cleanup } = await mount()
    await vi.waitFor(() => {
      const call = LAYUI.table.reloadData.mock.calls.find((item) => item[0] === SHOPS_TABLE_ID)
      expect(call, '店铺子表没有 reloadData').toBeTruthy()
      const payload = call![1] as { data: XgjShopsListRow[] }
      expect(payload.data).toHaveLength(1)
      expect(payload.data[0]!.shop_name).toBe('甲店')
      expect(payload.data[0]!.group_name).toBe('甲组')
    })
    cleanup()
  })
})
