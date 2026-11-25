// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OrderSourceRow } from '@/api/orderSources'
import type { ShopBindingRow } from '@/api/shopBindings'

/**
 * 【其它店商平台对接】island 页（`pages/system-pddshipsetting.ts`）的契约测试。
 *
 * 这一页**此前一个测试都没有**（spec §7.4 实测：`pddshipsetting` 只出现在菜单 / 路由的通用断言里），
 * 而本轮把它从「`artifactPage` 纯外观」改成了「自挂载 + 真 API」（spec §7.1.1）⇒ 新建本文件。
 *
 * 手法照 `system-sourelist.spec.ts`（同一条困境的先例）：
 *  - `vi.mock` 掉 `@/layui/island` 的 `bootstrapIsland`（jsdom 里加载不了真 layui）与两个 api 模块，
 *    只断言**入参**；
 *  - **证据件走真文件**（`public/replica/system-pddshipsetting.html`），不另造一份标记——否则
 *    「`lay-event` 名 / 弹层 DOM 名 / R8 那行到底改成什么样」这类断言会与真实产物脱钩；
 *  - 挂载走真 `mountConfigLike`（页面自己不再 `table.render`，见实现文件头）。
 *
 * ⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 * ⚠️ 本文件**不是** brief Step 2 里那段 `expect(fetchShopBindings).toBeDefined()` 脚手架
 * （GAP-2 的裁定：那只是行为意图，写下去就是永远绿的假测试）——下面的断言逐条钉**值**。
 */
vi.mock('@/api/shopBindings', () => ({
  fetchShopBindings: vi.fn(),
  fetchPlatformConfig: vi.fn(),
  createShopBinding: vi.fn(),
  updateShopBinding: vi.fn(),
  deleteShopBinding: vi.fn(),
}))

// 页面的「对应订单来源」下拉与 `soureName` 列要用来源字典（后端只回 `order_source` **id**）。
vi.mock('@/api/orderSources', () => ({ fetchOrderSources: vi.fn() }))

/**
 * layui 的**可控替身**。
 *
 * `form.val` 刻意实现成「按 `<form lay-filter>` 的作用域找输入」——这正是「先预填、再 open」
 * 那条硬要求的判据：`openPanelLayer` 会把 `.modifyParamDiv` 搬出 `<form>`（搬进
 * `.layui-layer-content`），一旦顺序写反，替身就找不到这些输入。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableHandlers = new Map<string, (...args: unknown[]) => unknown>()
  const formVals: Array<Record<string, unknown>> = []
  const open = vi.fn<(config: Record<string, unknown>) => number>(() => 1)
  return {
    msg,
    tableHandlers,
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
      on: vi.fn(),
    },
    table: {
      render: vi.fn(),
      reloadData: vi.fn(),
      on: (e: string, cb: (...args: unknown[]) => unknown) => tableHandlers.set(e, cb),
    },
    layer: { msg, open, close: vi.fn(), confirm: vi.fn(), prompt: vi.fn(), tips: vi.fn() },
    element: { init: vi.fn() },
    /** `layer.open({type:1, content})` 要的是 **jQuery 对象**（`config-replica:335`）：替身只包一层。 */
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

import { BizError } from '@/api/errors'
import { fetchOrderSources } from '@/api/orderSources'
import {
  createShopBinding,
  deleteShopBinding,
  fetchPlatformConfig,
  fetchShopBindings,
  updateShopBinding,
} from '@/api/shopBindings'
import {
  bindingDialogOptions,
  buildPddshipsettingCols,
  mountPddshipsettingPage,
  notOpenLabel,
  PANEL_SELECTOR,
  resolveOrderSource,
  TABLE_ID,
  toBindingRow,
  validateBindingForm,
} from '../system-pddshipsetting'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/system-pddshipsetting.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/system-pddshipsetting.html'),
  'utf8',
)

/** 一行真实形状的列表载荷（字段集 = `ShopBindingSerializer`，E-0253 起授权码只回布尔）。 */
const ROW: ShopBindingRow = {
  id: 7,
  platform: 20,
  platform_label: '闲鱼',
  platform_app_id: 'app-123',
  shop_name: '我的店',
  shop_id: '',
  order_source: 3,
  access_token_set: true,
  remark: '备注',
}

/** 一个真实的来源字典行（`OrderSourceSerializer`）。 */
const SOURCE: OrderSourceRow = {
  id: 3,
  name: '闲鱼渠道',
  remark: '',
  order_by: 0,
  shop_type: 20,
  group: null,
  group_name: '',
  created_at: '2026-09-21T10:00:00+08:00',
  updated_at: '2026-09-21T10:00:00+08:00',
}

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/**
 * 挂载整页：证据件走全局 fetch（**真文件**），列表与字典走 api 替身。
 * 挂载是异步的（`mountConfigLike` 取证据件 + 页面在 `onReady` 里起取数）⇒ 等到表格真被填上。
 */
async function mount(
  options: { rows?: ShopBindingRow[]; sources?: OrderSourceRow[] } = {},
): Promise<{ root: HTMLElement; cleanup: () => void; cfg: Record<string, unknown> }> {
  const rows = options.rows ?? [ROW]
  vi.mocked(fetchShopBindings).mockResolvedValue({
    items: rows,
    total: rows.length,
    page: 1,
    page_size: 200,
  })
  const sources = options.sources ?? [SOURCE]
  vi.mocked(fetchOrderSources).mockResolvedValue({
    items: sources,
    total: sources.length,
    page: 1,
    page_size: 200,
  })
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
  await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
  const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
  return { root, cleanup, cfg }
}

/** 表格里**真正渲染出去**的行（layui 的行内事件就是把这些行交回 `table.on('tool(…)')`）。 */
function renderedRows(): Array<Record<string, unknown>> {
  const call = LAYUI.table.reloadData.mock.calls[LAYUI.table.reloadData.mock.calls.length - 1]
  expect(call, '`table.reloadData` 没被调用').toBeTruthy()
  return (call![1] as { data: Array<Record<string, unknown>> }).data
}

/** 点一次工具栏事件（layui 的 `table.on('toolbar(tableList)')`）。 */
function fireToolbar(event: string): void {
  const cb = LAYUI.tableHandlers.get(`toolbar(${TABLE_ID})`)
  expect(cb, 'toolbar 处理器没登记').toBeTruthy()
  cb!({ event })
}

/** 点一次行内事件（layui 的 `table.on('tool(tableList)')`）。 */
function fireTool(event: string, row: Record<string, unknown>): void {
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

/** 在页内点一个元素（`bubbles: true`：「尚未开放」与保存都靠 root 之外的捕获委托）。 */
function click(el: Element): void {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

/** 点弹层里的保存按钮（`<a class="… saveInterfaceBtn">保存设置</a>`，**不是** `lay-submit`）。 */
function clickSave(panel: HTMLElement): void {
  click(panel.querySelector('.saveInterfaceBtn')!)
}

function panelInput(panel: HTMLElement, name: string): HTMLInputElement {
  const el = panel.querySelector<HTMLInputElement>(`[name="${name}"]`)
  expect(el, `弹层里没有 [name="${name}"]`).toBeTruthy()
  return el!
}

beforeEach(() => {
  LAYUI.table.render.mockClear()
  LAYUI.table.reloadData.mockClear()
  LAYUI.msg.mockClear()
  // ⚠️ `mockReset`（不是 `mockClear`）：有的用例会临时换掉 `open` 的实现（模拟 layui 把节点
  //    搬进 `.layui-layer-content`）；`mockClear` 只清调用记录、**保留实现** ⇒ 会把「搬走」泄漏给
  //    后面的用例（先例：`system-sourelist.spec.ts:194-197` 实测过这条假红）。
  LAYUI.open.mockReset()
  LAYUI.open.mockReturnValue(1)
  LAYUI.layer.close.mockClear()
  LAYUI.layer.confirm.mockClear()
  LAYUI.form.render.mockClear()
  LAYUI.form.val.mockClear()
  LAYUI.formVals.length = 0
  LAYUI.tableHandlers.clear()
  vi.mocked(fetchShopBindings).mockReset()
  vi.mocked(fetchPlatformConfig).mockReset()
  vi.mocked(fetchOrderSources).mockReset()
  vi.mocked(createShopBinding).mockReset()
  vi.mocked(updateShopBinding).mockReset()
  vi.mocked(deleteShopBinding).mockReset()
  vi.mocked(createShopBinding).mockResolvedValue(ROW)
  vi.mocked(updateShopBinding).mockResolvedValue(ROW)
  vi.mocked(deleteShopBinding).mockResolvedValue(null)
  // 平台配置端点的**默认**成功值：与 `ROW.platform_app_id` 同为 `'app-123'`，
  // 这样既有用例（断言 `#appId` = `'app-123'`）在新旧两条取值路径下都成立；
  // 「零绑定也要显示」与「取不到时退回逐行值」由专门的新用例钉住。
  vi.mocked(fetchPlatformConfig).mockResolvedValue({ platform_app_id: 'app-123' })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('system-pddshipsetting.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/* ───────────────────────── 证据件：R8 与结构（spec §7.1 / §7.1.1） ───────────────────────── */

describe('证据件：R8 清空对标租户数据 + 弹层/按钮结构与对标逐字', () => {
  it('R8：「剩余调用量」与同一个 href 里的对标租户 ID 都置空，`28653` / `u=66253` 一处不剩', () => {
    expect(EVIDENCE).not.toContain('28653')
    expect(EVIDENCE).not.toContain('u=66253')
    // 结构保留：还是那一行、还是那个 `payBtn` 链接与 `u=` 参数（值置空，见 spec §7.3-P4）。
    expect(EVIDENCE).toContain(
      '<a class="payBtn" href="https://www.jidanpu.com/buy/buytaobaoservice?u=" target="_blank"',
    )
    expect(EVIDENCE).toContain('（当前系统接口剩余调用量：，接口读取订单会消耗调用量）')
  })

  it('弹层容器是 `.modifyParamDiv`（**只有 class、没有 id**、初始 `display:none`）', () => {
    const doc = new DOMParser().parseFromString(EVIDENCE, 'text/html')
    const panel = doc.querySelector('.modifyParamDiv')
    expect(panel).toBeTruthy()
    expect(panel!.id).toBe('')
    expect(panel!.getAttribute('style')).toContain('display:none')
    // 弹层的 DOM 名逐字照对标（spec §2.4）：平台单选叫 `platforms`（**不是** `platformType`）。
    expect(panel!.querySelectorAll('input[name="platforms"]')).toHaveLength(5)
    for (const id of ['appId', 'appSecret', 'accessToken', 'shopSellerNick', 'remark', 'tbsoure']) {
      expect(panel!.querySelector(`#${id}`), `弹层里没有 #${id}`).toBeTruthy()
    }
    expect(panel!.querySelector('.appSecretDiv')).toBeTruthy()
    expect(panel!.querySelector('.getAuth')).toBeTruthy()
    // ⚠️ 保存按钮**不是** `lay-submit`（对标是 `<a class="… saveInterfaceBtn">`）⇒ 页面自挂点击。
    expect(panel!.querySelector('.saveInterfaceBtn')!.getAttribute('lay-submit')).toBeNull()
    // 订单来源下拉的首项是「不设置」（其余槽位由页面运行期填）。
    expect(panel!.querySelectorAll('select#tbsoure option')[0]!.getAttribute('value')).toBe('')
    expect(panel!.querySelectorAll('select#tbsoure option')[0]!.textContent).toBe('不设置')
  })

  it('工具栏/行内按钮的 `lay-event` 逐字：refresh / addBtn / submitEdit / submitDel', () => {
    const doc = new DOMParser().parseFromString(EVIDENCE, 'text/html')
    // ⚠️ 两个模板是 `<script type="text/html">`：浏览器/DOMParser 都按**文本**处理它的内容，
    //    所以必须把 `textContent` 再解析一遍才看得到里面的元素（先例 `game-projectsetting.spec.ts`）。
    const events = (id: string): Array<string | null> => {
      const inner = new DOMParser().parseFromString(
        doc.querySelector(`#${id}`)!.textContent ?? '',
        'text/html',
      )
      return Array.from(inner.querySelectorAll('[lay-event]')).map((el) =>
        el.getAttribute('lay-event'),
      )
    }
    expect(events('toolbarDemo')).toEqual(['refresh', 'addBtn'])
    expect(events('baroperating')).toEqual(['submitEdit', 'submitDel'])
  })

  it('「保存以上设置」（`lay-submit lay-filter="savebtn"`）与「获取授权」仍在页内（照渲染、不置灰）', () => {
    const doc = new DOMParser().parseFromString(EVIDENCE, 'text/html')
    const save = doc.querySelector('button[lay-filter="savebtn"]')
    expect(save).toBeTruthy()
    expect(save!.textContent).toBe('保存以上设置')
    expect(save!.hasAttribute('disabled')).toBe(false)
    expect(doc.querySelector('a.getAuth')!.textContent).toBe('获取授权')
  })
})

/* ───────────────────────── 列定义与 templet（spec §2.3 / §7.1） ───────────────────────── */

describe('列定义：8 列骨架逐条照对标，`platformType` 补 templet、操作列挂 `#baroperating`', () => {
  const cols = buildPddshipsettingCols()
  const byField = (field: string): Record<string, unknown> => cols.find((c) => c.field === field)!

  it('8 列，顺序/标题/宽度/对齐逐条一致（备注列无 `width` = 自动列）', () => {
    expect(cols.map((c) => c.title)).toEqual([
      '平台',
      '店铺ID或昵称',
      '订单来源匹配',
      'AppId',
      'AppSecret',
      'AccessToken',
      '备注说明',
      '操作',
    ])
    expect(byField('platformType').width).toBe(100)
    expect(byField('platformType').align).toBe('center')
    expect(byField('shopName').width).toBe(200)
    expect(byField('soureName').width).toBe(200)
    expect(byField('appId').width).toBe(170)
    expect(byField('appId').align).toBe('left')
    expect(byField('appSecret').width).toBe(250)
    expect(byField('accessToken').width).toBe(355)
    expect(byField('remark').width).toBeUndefined()
    expect(byField('remark').align).toBe('left')
    expect(byField('操作')).toBeUndefined()
    expect(cols[7]!.title).toBe('操作')
    expect(cols[7]!.width).toBe(160)
    expect(cols[7]!.align).toBe('center')
  })

  it('操作列挂 `toolbar: \'#baroperating\'`（漏了它 = 行内没有「修改 / 删除」两颗按钮）', () => {
    expect(cols[7]!.toolbar).toBe('#baroperating')
    expect(cols[7]!.field).toBeUndefined()
  })

  it('`platformType` 的 templet 用 `platform_label` 渲染（后端给的平台文案），空值退化成空 `<span>`', () => {
    const cell = byField('platformType').templet as (row: Record<string, unknown>) => string
    expect(cell({ platform_label: '闲鱼' })).toBe('<span>闲鱼</span>')
    expect(cell({})).toBe('<span></span>')
  })

  it('`platform_label` 被 HTML 转义（`templet` 的返回值被 layui **当 HTML 插入**）', () => {
    const cell = byField('platformType').templet as (row: Record<string, unknown>) => string
    const html = cell({ platform_label: '<img src=x onerror=alert(1)>' })
    expect(html).toBe('<span>&lt;img src=x onerror=alert(1)&gt;</span>')
    expect(html).not.toContain('<img')
  })

  it('`appSecret` 列**没有** templet（我们每行没有 secret：`toBindingRow` 置空，见其注释）', () => {
    expect(byField('appSecret').templet).toBeUndefined()
  })
})

/* ───────────────────────── 行映射（spec §6.1 的字段 → 对标的列名） ───────────────────────── */

describe('toBindingRow：接口 snake_case → 对标的列名，并把来源 id 解析成名称', () => {
  it('逐字段映射（含 `platform_label` 原样留在行里给 templet 用）', () => {
    const names = new Map<number, string>([[3, '闲鱼渠道']])
    expect(toBindingRow(ROW, names)).toEqual({
      id: 7,
      platform: 20,
      platform_label: '闲鱼',
      platform_app_id: 'app-123',
      shop_name: '我的店',
      shop_id: '',
      order_source: 3,
      access_token_set: true,
      remark: '备注',
      platformType: 20,
      shopName: '我的店',
      soureName: '闲鱼渠道',
      appId: 'app-123',
      appSecret: '',
      accessToken: '已配置',
    })
  })

  it('`accessToken` 列按布尔渲染「已配置 / 未配置」（E-0253：接口没有字面值可显示）', () => {
    expect(toBindingRow(ROW, new Map()).accessToken).toBe('已配置')
    expect(toBindingRow({ ...ROW, access_token_set: false }, new Map()).accessToken).toBe('未配置')
  })

  it('`appSecret` 恒空（spec §11-T7「那一格显示什么」未经核实 ⇒ **推定**为不显示）', () => {
    const row = toBindingRow(ROW, new Map())
    expect(row.appSecret).toBe('')
  })

  it('`order_source` 为 `null`（不设置）或字典里查不到（来源被删）⇒ 该列空，不写 `null`/`undefined`', () => {
    expect(toBindingRow({ ...ROW, order_source: null }, new Map([[3, '闲鱼渠道']])).soureName).toBe('')
    expect(toBindingRow(ROW, new Map()).soureName).toBe('')
  })
})

/* ───────────────────────── 弹层参数与校验（`pddshipsetting.js` L154-177 / L303-322） ───────────────────────── */

describe('弹层参数与保存校验', () => {
  it('`bindingDialogOptions` 逐项照对标（桌面分支 `area`、`shadeClose:false`）', () => {
    expect(bindingDialogOptions(false, 'CONTENT')).toEqual({
      title: '新增一个店铺对接',
      shade: [0.3, '#333'],
      shadeClose: false,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['870px', '680px'],
      content: 'CONTENT',
    })
    expect(bindingDialogOptions(true, 'CONTENT').title).toBe('编辑对接')
  })

  it('平台必选 → 「请选择一个店铺所在的平台」（文案逐字取自对标 L316）', () => {
    expect(validateBindingForm({ platform: null })).toEqual({
      error: '请选择一个店铺所在的平台',
      platform: 0,
    })
  })

  it('通过 → `error` 为空串（AppSecret 那条在本页不可能触发：AppId 只读且恒等于平台值）', () => {
    expect(validateBindingForm({ platform: 20 })).toEqual({ error: '', platform: 20 })
  })
})

/* ───────── `order_source` 载荷判据（评审修复轮 1 #2：字典取不到时**不许**清掉已有绑定） ───────── */

describe('resolveOrderSource：`undefined` = 不带这个键（保留原值），`null` = 用户主动清空', () => {
  const KNOWN = new Set([3, 9])

  it('字典**没取到过** ⇒ 不带（下拉的空串是「显示不出来」，不是「用户选了不设置」）', () => {
    expect(
      resolveOrderSource({ selected: '', sourcesLoaded: false, existing: 3, knownSourceIds: KNOWN }),
    ).toBeUndefined()
    // 连「下拉里居然有个数字」也不信：那说明我们根本不知道这份选项是哪来的。
    expect(
      resolveOrderSource({ selected: '3', sourcesLoaded: false, existing: 3, knownSourceIds: KNOWN }),
    ).toBeUndefined()
  })

  it('字典取到了、用户**主动**选「不设置」⇒ 发 `null`（清空这条路必须留着）', () => {
    expect(
      resolveOrderSource({ selected: '', sourcesLoaded: true, existing: 3, knownSourceIds: KNOWN }),
    ).toBeNull()
  })

  it('已有绑定的来源 id 本页认不出（字典陈旧 / 被 200 条上限截断）⇒ 不带', () => {
    expect(
      resolveOrderSource({ selected: '', sourcesLoaded: true, existing: 5, knownSourceIds: KNOWN }),
    ).toBeUndefined()
  })

  it('正常情况下照发数字 id（写成字面值，不自指）', () => {
    expect(
      resolveOrderSource({ selected: '9', sourcesLoaded: true, existing: 3, knownSourceIds: KNOWN }),
    ).toBe(9)
    // 新增（existing 为 null）时同样照发。
    expect(
      resolveOrderSource({ selected: '9', sourcesLoaded: true, existing: null, knownSourceIds: KNOWN }),
    ).toBe(9)
  })

  it('畸形值既不猜也不折成 0（0 不是合法来源 id）', () => {
    expect(
      resolveOrderSource({ selected: 'abc', sourcesLoaded: true, existing: null, knownSourceIds: KNOWN }),
    ).toBeUndefined()
    expect(
      resolveOrderSource({ selected: '0', sourcesLoaded: true, existing: null, knownSourceIds: KNOWN }),
    ).toBeUndefined()
    expect(
      resolveOrderSource({ selected: '-1', sourcesLoaded: true, existing: null, knownSourceIds: KNOWN }),
    ).toBeUndefined()
  })

  /**
   * **修复轮 2 #1：用户的选择优先于「原值认不出」这条护栏。**
   *
   * 已有来源 id 本页认不出（字典陈旧 / 来源数 > `page_size=200` 被截断）时，若把「认不出」
   * 的护栏排在用户选择**之前**，用户**主动改选了一个认得的新来源**也会走成「不带键」⇒
   * PATCH 缺键 ⇒ 界面提示保存成功、**来源根本没变**（这是修复轮 1 引入的窄回归）。
   * 期望值写字面值（E-0024）。
   */
  it('已有来源 **认不出** + 用户主动改选一个**认得**的新来源 ⇒ 发那个新 id（不是 undefined / null）', () => {
    expect(
      resolveOrderSource({ selected: '9', sourcesLoaded: true, existing: 5, knownSourceIds: KNOWN }),
    ).toBe(9)
    expect(
      resolveOrderSource({ selected: '3', sourcesLoaded: true, existing: 5, knownSourceIds: KNOWN }),
    ).toBe(3)
  })

  it('已有来源认得出 + 用户改选另一个认得的来源 ⇒ 同样听用户的', () => {
    expect(
      resolveOrderSource({ selected: '9', sourcesLoaded: true, existing: 3, knownSourceIds: KNOWN }),
    ).toBe(9)
  })
})

/* ───────────────────────── 挂载与取数 ───────────────────────── */

describe('挂载：走 `mountConfigLike`（页面自己取数），表格声明照对标', () => {
  it('`table.render`：elem/id/toolbar/defaultToolbar/page/height 与 `data: []` 先渲染一次', async () => {
    const { cleanup, cfg } = await mount()
    expect(cfg.elem).toBe('#tableList')
    // `reloadData` 按**实例 id** 查表（`config-replica.ts:116-122`）：先 render 一次 + 显式 id。
    expect(cfg.id).toBe(TABLE_ID)
    expect(cfg.toolbar).toBe('#toolbarDemo')
    expect(cfg.defaultToolbar).toEqual(['filter'])
    expect(cfg.page).toBe(false)
    expect(cfg.height).toBe('full')
    expect(cfg.data).toEqual([])
    expect((cfg.cols as unknown[][])[0]).toHaveLength(8)
    cleanup()
  })

  it('首屏：`fetchShopBindings` 一次，21 条一行不少地交给 `table.reloadData`', async () => {
    const rows = Array.from({ length: 21 }, (_, i) => ({ ...ROW, id: i + 1, shop_name: `店${i + 1}` }))
    const { cleanup } = await mount({ rows })
    expect(fetchShopBindings).toHaveBeenCalledTimes(1)
    // 平台配置端点与列表**同一轮**取（并进同一个 `Promise.all`），只取一次。
    expect(fetchPlatformConfig).toHaveBeenCalledTimes(1)
    const call = LAYUI.table.reloadData.mock.calls[0]!
    expect(call[0]).toBe(TABLE_ID)
    const data = (call[1] as { data: Array<Record<string, unknown>> }).data
    expect(data).toHaveLength(21)
    // 字面值钉首尾两行（E-0024：不只数长度）。
    expect(data[0]).toMatchObject({ id: 1, platformType: 20, shopName: '店1', soureName: '闲鱼渠道' })
    expect(data[20]).toMatchObject({ id: 21, platformType: 20, shopName: '店21' })
    cleanup()
  })

  it('订单来源字典一次取回、灌进弹层的下拉（含「不设置」首项）；名称进 `soureName` 列', async () => {
    const { root, cleanup } = await mount()
    expect(fetchOrderSources).toHaveBeenCalledTimes(1)
    const select = root.querySelector<HTMLSelectElement>('select#tbsoure')!
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['', '3'])
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['不设置', '闲鱼渠道'])
    expect(renderedRows()[0]!.soureName).toBe('闲鱼渠道')
    cleanup()
  })

  it('来源名被转义（`<option>` 是 innerHTML 拼的，来源名是用户输入）', async () => {
    const { root, cleanup } = await mount({
      sources: [{ ...SOURCE, name: '"><img src=x>' }],
    })
    const option = root.querySelector<HTMLOptionElement>('select#tbsoure option[value="3"]')!
    expect(option.textContent).toBe('"><img src=x>')
    expect(root.querySelector('select#tbsoure')!.innerHTML).not.toContain('<img')
    cleanup()
  })

  it('字典取不到**不阻断**页面：下拉只剩「不设置」，表格照常渲染', async () => {
    vi.mocked(fetchOrderSources).mockRejectedValue(new BizError(1002, '取字典失败'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchShopBindings).mockResolvedValue({ items: [ROW], total: 1, page: 1, page_size: 200 })
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
    expect(renderedRows()).toHaveLength(1)
    expect(root.querySelectorAll('select#tbsoure option')).toHaveLength(1)
    cleanup()
  })

  it('取数失败 → `msg(e.message)`，不抛、不留半张表', async () => {
    vi.mocked(fetchShopBindings).mockRejectedValue(new BizError(1002, '分页参数非法'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('分页参数非法', { time: 1500 }))
    expect(LAYUI.table.reloadData).not.toHaveBeenCalled()
    cleanup()
  })

  it('刷新按钮 → 只是再取一次数（不弹窗）', async () => {
    const { cleanup } = await mount()
    fireToolbar('refresh')
    await vi.waitFor(() => expect(fetchShopBindings).toHaveBeenCalledTimes(2))
    expect(LAYUI.open).not.toHaveBeenCalled()
    cleanup()
  })
})

/* ───────────────────────── 弹层：打开与预填 ───────────────────────── */

describe('弹层：layer.open 参数 + 初值 + 编辑预填 + AppId 只读', () => {
  it('「新增一个店铺对接」→ 平台未选、AppId 预填平台值、其余为空；`.appSecretDiv` 隐藏', async () => {
    const { root, cleanup } = await mount()
    fireToolbar('addBtn')
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const config = lastOpenConfig()
    expect(config.title).toBe('新增一个店铺对接')
    expect(config.type).toBe(1)
    expect(config.shade).toEqual([0.3, '#333'])
    expect(config.shadeClose).toBe(false)
    expect(config.skin).toBe('layui-layer-lan')
    expect(config.anim).toBe(8)
    expect(config.area).toEqual(['870px', '680px'])
    expect(LAYUI.formVals[0]).toEqual({
      platforms: 0,
      appId: 'app-123',
      appSecret: '',
      accessToken: '',
      tbsoure: '',
      remark: '',
      shopSellerNick: '',
    })
    const panel = openedPanel()
    // 平台单选被**取消选中**（`platforms: 0` 没有对应 radio）⇒ 逼用户选。
    expect(panel.querySelector('[name="platforms"]:checked')).toBeNull()
    cleanup()
    void root
  })

  it('`#appId` **只读**且已回填平台 AppId；平台密钥那一行不出现（`display:none`）', async () => {
    const { root, cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    const appId = panel.querySelector<HTMLInputElement>('#appId')!
    expect(appId.readOnly).toBe(true)
    expect(appId.hasAttribute('readonly')).toBe(true)
    expect(appId.value).toBe('app-123')
    expect(panel.querySelector<HTMLElement>('.appSecretDiv')!.style.display).toBe('none')
    // 平台密钥**一次都不进浏览器**：整页渲染结果里没有 secret 一词的值（行仍在，值恒空）。
    expect(panel.querySelector<HTMLInputElement>('#appSecret')!.value).toBe('')
    cleanup()
    void root
  })

  it('行内「修改」→ 用**表格真正渲染出去的那一行**回填（平台/店铺/备注/来源）；授权码**不预填**（E-0253：接口没有字面值），标题「编辑对接」', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', renderedRows()[0]!)
    expect(lastOpenConfig().title).toBe('编辑对接')
    expect(LAYUI.formVals[0]).toEqual({
      platforms: 20,
      appId: 'app-123',
      appSecret: '',
      accessToken: '',
      tbsoure: 3,
      remark: '备注',
      shopSellerNick: '我的店',
    })
    const panel = openedPanel()
    expect(panel.querySelector<HTMLInputElement>('[name="platforms"][value="20"]')!.checked).toBe(true)
    expect(panel.querySelector<HTMLSelectElement>('select#tbsoure')!.value).toBe('3')
    cleanup()
  })

  it('**先预填、再 open**：`layer.open` 在 `form.val` / `form.render` 之后', async () => {
    const { cleanup } = await mount()
    // ⚠️ 先把要点的行取下来：下面 `clearAllMocks()` 会清掉 `reloadData` 的调用记录，
    //    之后再调 `renderedRows()` 就取不到了（第一版就是这么写的，会以「没被调用」假红）。
    const row = renderedRows()[0]!
    vi.clearAllMocks()
    LAYUI.formVals.length = 0
    // 真 layui 的 `layer.open({type:1, content: jq})` 会把节点**搬出 `<form>`**；替身照此实现，
    // 于是「顺序写反」时真 `form.val` 的作用域会找不到这些输入（编辑时打开是空表单）。
    LAYUI.open.mockImplementation(() => {
      const panel = document.querySelector<HTMLElement>(PANEL_SELECTOR)
      const holder = document.createElement('div')
      holder.className = 'layui-layer-content'
      document.body.appendChild(holder)
      if (panel) holder.appendChild(panel)
      return 1
    })
    fireTool('submitEdit', row)
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(LAYUI.form.val.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.open.mock.invocationCallOrder[0]!,
    )
    expect(LAYUI.form.render.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.open.mock.invocationCallOrder[0]!,
    )
    cleanup()
  })
})

/* ───────────────────────── 保存 / 删除 ───────────────────────── */

describe('保存与删除：真发请求、失败原样显示、成功关窗刷新', () => {
  it('新增：POST 载荷 = `{platform, shop_name, order_source, access_token, remark}`', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    panel.querySelector<HTMLInputElement>('[name="platforms"][value="20"]')!.checked = true
    panelInput(panel, 'shopSellerNick').value = '新店'
    panelInput(panel, 'accessToken').value = 'tok-new'
    panelInput(panel, 'remark').value = '备注新'
    clickSave(panel)
    await vi.waitFor(() => expect(createShopBinding).toHaveBeenCalledTimes(1))
    expect(createShopBinding).toHaveBeenCalledWith({
      platform: 20,
      shop_name: '新店',
      order_source: null,
      access_token: 'tok-new',
      remark: '备注新',
    })
    cleanup()
  })

  it('编辑：走 **PATCH**（`urls.py` 没有 PUT）且 id 取当前行；授权码留空 ⇒ 发空串（后端 = 不改）；成功后关窗 + 刷新 + 「保存成功」', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', renderedRows()[0]!)
    clickSave(openedPanel())
    await vi.waitFor(() => expect(updateShopBinding).toHaveBeenCalledTimes(1))
    expect(updateShopBinding).toHaveBeenCalledWith(7, {
      platform: 20,
      shop_name: '我的店',
      order_source: 3,
      // E-0253 起编辑弹层不再预填授权码 ⇒ 原样保存发的是空串，后端 PATCH 空串 = 不改。
      access_token: '',
      remark: '备注',
    })
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    await vi.waitFor(() => expect(fetchShopBindings).toHaveBeenCalledTimes(2))
    expect(LAYUI.msg).toHaveBeenCalledWith('保存成功', { time: 1500 })
    cleanup()
  })

  it('平台没选 → 只提示、**不发请求、不关窗**', async () => {
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    clickSave(openedPanel())
    await Promise.resolve()
    expect(LAYUI.msg).toHaveBeenCalledWith('请选择一个店铺所在的平台', { time: 1500 })
    expect(createShopBinding).not.toHaveBeenCalled()
    expect(updateShopBinding).not.toHaveBeenCalled()
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    cleanup()
  })

  /**
   * **零绑定也要看得到 AppId**（这次修复的核心行为）。
   *
   * `platform_app_id` 是逐行字段：租户尚无任何对接时列表为空 ⇒ 只读的 `#appId` 原本是空的。
   * 现在优先取与「行」解耦的 `platform-config` 端点 ⇒ 列表为空也有值。
   * 判据：`#appId` 的值 = 端点回的那个 AppId **字面量**（不是空串），且仍 `readOnly`。
   */
  it('列表为空、`platform-config` 回了 AppId ⇒ 弹层 `#appId` 的值就是它（仍只读）', async () => {
    vi.mocked(fetchShopBindings).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
    vi.mocked(fetchPlatformConfig).mockResolvedValue({ platform_app_id: 'app-zero-binding' })
    const root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchOrderSources).mockResolvedValue({
      items: [SOURCE],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
    // 前置事实（判别力的来源）：**一行对接都没有** ⇒ 逐行路径给不出任何 AppId。
    expect(renderedRows()).toHaveLength(0)
    fireToolbar('addBtn')
    const appId = openedPanel().querySelector<HTMLInputElement>('#appId')!
    // 字面量钉值（不 import 被测常量；`AGENTS.md` §5 / E-0024）。
    expect(appId.value).toBe('app-zero-binding')
    expect(appId.readOnly).toBe(true)
    cleanup()
  })

  /**
   * **优先级守卫**：`platform-config` 有值 ⇒ 用它，**不**用列表首行的逐行值。
   *
   * spec §7.1 原文「AppId 由列表响应的 `platform_app_id` 只读预填」只覆盖「列表有行」的情形；
   * 本用例把「端点有值、逐行值也在、且两者**不同**」这一格钉死——优先级一旦被反转（改用逐行值），
   * 它必须红。实测（评审临时探针）：把这行优先级删掉，全量测试此前**不会红**。
   */
  it('优先级：`platform-config` 与列表首行的逐行值不同 ⇒ 用**端点**那个', async () => {
    vi.mocked(fetchPlatformConfig).mockResolvedValue({ platform_app_id: 'endpoint-wins' })
    const { cleanup } = await mount({ rows: [ROW] })
    // 前置事实（判别力来源）：逐行值确实存在、且与端点值不同（相同或都为空都测不出优先级）。
    expect(renderedRows()[0]!.platform_app_id).toBe('app-123')
    fireToolbar('addBtn')
    const appId = openedPanel().querySelector<HTMLInputElement>('#appId')!
    // 字面量钉值（不 import 被测常量；`AGENTS.md` §5 / E-0024）。
    expect(appId.value).toBe('endpoint-wins')
    cleanup()
  })

  /**
   * **兜底守卫**：`platform-config` 回**空串**（平台未配置）⇒ 退回列表有行时的逐行
   * `platform_app_id`。把这条兜底关掉（只认端点值）时必须红——否则「端点未配置」的租户
   * 在有行的情况下反而比修复前更糟（弹层 AppId 变空）。
   */
  it('兜底：`platform-config` 回空串 + 列表有行 ⇒ 用该行的逐行 `platform_app_id`', async () => {
    vi.mocked(fetchPlatformConfig).mockResolvedValue({ platform_app_id: '' })
    const { cleanup } = await mount({ rows: [ROW] })
    // 前置事实（判别力来源）：这一行的逐行值是**非空**的 `'app-123'`（空值时本用例无判别力）。
    expect(renderedRows()[0]!.platform_app_id).toBe('app-123')
    fireToolbar('addBtn')
    const appId = openedPanel().querySelector<HTMLInputElement>('#appId')!
    expect(appId.value).toBe('app-123')
    cleanup()
  })

  /**
   * **「第一条对接建得出来」的守卫**（spec §7.1 的更正：前端**不**校验 AppId 必填）。
   *
   * ⚠️ **前置事实已随本次修复改过**（改动原因写在这里）：原文说「列表为空 ⇒ 只读的 `#appId`
   * 是空的」，但**列表为空现在不再导致空值**——弹层优先取与「行」解耦的 `platform-config`
   * 端点（上面的新用例钉住的就是它）。所以这里把 AppId 的**两条**来源都拿掉：
   * 列表为空 **且** `platform-config` 取失败。**意图一个字没变**：
   * AppId 拿不到时保存照常发出去（「平台 AppId 没配」由读单端点的 `1015` 表达）；
   * 若照抄对标的「AppId 必填」，这个租户会被**永久卡死**（字段只读、他无从自救）。
   */
  it('平台 AppId 两条来源都拿不到（列表空 + `platform-config` 失败）⇒ 保存**不被拦**，第一条对接照建', async () => {
    vi.mocked(fetchShopBindings).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
    // 新端点失败 ⇒ 退回逐行值；列表为空 ⇒ 逐行值也没有 ⇒ `#appId` 为空。
    vi.mocked(fetchPlatformConfig).mockRejectedValue(new BizError(1002, '平台配置取不到'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchOrderSources).mockResolvedValue({
      items: [SOURCE],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
    // 预填值取不到**不许**变成整页错误：列表照常渲染、页面不弹这条 message。
    expect(LAYUI.msg).not.toHaveBeenCalledWith('平台配置取不到', { time: 1500 })
    fireToolbar('addBtn')
    const panel = openedPanel()
    // 前置事实（判据的判别力来源）：两条来源都没有 ⇒ 只读的 AppId 是**空**的。
    expect(panel.querySelector<HTMLInputElement>('#appId')!.value).toBe('')
    panel.querySelector<HTMLInputElement>('[name="platforms"][value="20"]')!.checked = true
    panelInput(panel, 'accessToken').value = 'tok-1'
    clickSave(panel)
    await vi.waitFor(() => expect(createShopBinding).toHaveBeenCalledTimes(1))
    expect(createShopBinding).toHaveBeenCalledWith({
      platform: 20,
      shop_name: '',
      order_source: null,
      access_token: 'tok-1',
      remark: '',
    })
    cleanup()
  })

  it('后端拒绝 → 原样显示 `e.message`（业务码 1001 走同一出口），不发「保存成功」、不关窗', async () => {
    vi.mocked(createShopBinding).mockRejectedValue(new BizError(1000, '本轮只支持闲鱼店铺对接'))
    const { cleanup } = await mount()
    fireToolbar('addBtn')
    const panel = openedPanel()
    panel.querySelector<HTMLInputElement>('[name="platforms"][value="10"]')!.checked = true
    clickSave(panel)
    await vi.waitFor(() =>
      expect(LAYUI.msg).toHaveBeenCalledWith('本轮只支持闲鱼店铺对接', { time: 1500 }),
    )
    expect(LAYUI.msg).not.toHaveBeenCalledWith('保存成功', { time: 1500 })
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    cleanup()
  })

  it('删除：确认语与按钮逐字照对标 L304-305 → `DELETE(id)` → 刷新', async () => {
    const { cleanup } = await mount()
    fireTool('submitDel', renderedRows()[0]!)
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    const [text, options, cb] = LAYUI.layer.confirm.mock.calls[0] as unknown as [
      string,
      Record<string, unknown>,
      (index: number) => void,
    ]
    expect(text).toBe('是否确认删除这个店铺对接？')
    expect(options).toEqual({ btn: ['确认删除', '点错了'] })
    cb(5)
    await vi.waitFor(() => expect(deleteShopBinding).toHaveBeenCalledWith(7))
    expect(LAYUI.layer.close).toHaveBeenCalledWith(5)
    await vi.waitFor(() => expect(fetchShopBindings).toHaveBeenCalledTimes(2))
    cleanup()
  })

  /**
   * **数据丢失方向的护栏**（评审修复轮 1 #2，评审用 jsdom 探针实测的失败形态）。
   *
   * 字典请求失败时下拉里只有「不设置」⇒ `<select>.value` 变成 `''`（没有可匹配的 option、
   * `selectedIndex = -1`），而这一行的 `order_source` 其实是 `3`。若照发这个 `''`（→ `null`），
   * 「只改备注保存一次」就会把**已有绑定静默清空**。
   *
   * 判据：编辑态 + 字典失败 + 保存 ⇒ 请求体的键里**没有** `order_source`（PATCH 缺键 = 不改），
   * 而不是 `order_source: null`。
   */
  it('⚠️ 字典取不到 + 编辑态 + 保存 ⇒ **不带** `order_source`（不把已有绑定静默清空）', async () => {
    vi.mocked(fetchOrderSources).mockRejectedValue(new BizError(1002, '取字典失败'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchShopBindings).mockResolvedValue({ items: [ROW], total: 1, page: 1, page_size: 200 })
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
    // 前置事实（判据的判别力来源）：下拉只剩「不设置」，它的 `value` 是空串。
    expect(root.querySelectorAll('select#tbsoure option')).toHaveLength(1)
    fireTool('submitEdit', renderedRows()[0]!)
    const panel = openedPanel()
    expect(panel.querySelector<HTMLSelectElement>('select#tbsoure')!.value).toBe('')
    clickSave(panel)
    await vi.waitFor(() => expect(updateShopBinding).toHaveBeenCalledTimes(1))
    const [id, payload] = vi.mocked(updateShopBinding).mock.calls[0]!
    expect(id).toBe(7)
    // ⚠️ 用**键集合**判「没带这个键」（`toEqual` 会忽略 `undefined`，那种写法下「带 null」和
    //    「不带」都可能绿；先例 `api/__tests__/orderSources.spec.ts:23-27` 记的同一个坑）。
    expect(Object.keys(payload).sort()).toEqual(['access_token', 'platform', 'remark', 'shop_name'])
    expect('order_source' in payload).toBe(false)
    // 保存本身照常成功（护栏不是「拦住保存」）。
    expect(LAYUI.msg).toHaveBeenCalledWith('保存成功', { time: 1500 })
    cleanup()
  })

  /**
   * **分支 2 的「接线」守卫**（修复轮 2 #2）。
   *
   * 上一条测的是**函数**；这一条测的是**页面真的把 `editing.order_source` 交进去了**——
   * 评审实测：把 `submit()` 里取 `existing` 的那处改成恒 `null`，上一条**照样绿**
   * （`existing: null` 时护栏不触发 ⇒ 发 `null` ⇒ 但上一条的前置是「字典失败」，
   * 而字典失败走的是第 1 条判据 `sourcesLoaded === false`，与 `existing` 无关）。
   * 那正是本仓明载的假通过形态：**「测了函数，没测接线」**。
   *
   * 构造：字典**正常**（`sourcesLoaded === true`），但列表这一行的 `order_source` 是**本页认不出**的
   * `5`（字典里只有 `3`）⇒ 下拉显示不出它、停在「不设置」。用户没动下拉就保存 ⇒ 必须**不带**这个键。
   */
  it('⚠️ 已有来源本页认不出（字典里没有它）+ 编辑态 + 保存 ⇒ **不带** `order_source`（接线判据）', async () => {
    vi.mocked(fetchShopBindings).mockResolvedValue({
      items: [{ ...ROW, order_source: 5 }],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchOrderSources).mockResolvedValue({
      items: [SOURCE],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
    // 前置事实（判别力来源）：字典**正常**（`sourcesLoaded === true`）但里面没有 5，
    // 所以下拉显示不出原有来源、停在「不设置」。
    expect(root.querySelectorAll('select#tbsoure option')).toHaveLength(2)
    const rendered = renderedRows()[0]!
    expect(rendered.order_source).toBe(5)
    expect(rendered.soureName).toBe('')
    fireTool('submitEdit', rendered)
    const panel = openedPanel()
    expect(panel.querySelector<HTMLSelectElement>('select#tbsoure')!.value).toBe('')
    clickSave(panel)
    await vi.waitFor(() => expect(updateShopBinding).toHaveBeenCalledTimes(1))
    const [id, payload] = vi.mocked(updateShopBinding).mock.calls[0]!
    expect(id).toBe(7)
    // 键集合判「没带」（不用 `toEqual`：它忽略 `undefined`）。
    expect(Object.keys(payload).sort()).toEqual(['access_token', 'platform', 'remark', 'shop_name'])
    expect('order_source' in payload).toBe(false)
    cleanup()
  })

  /**
   * **反向接线守卫**（修复轮 2 #1 的挂载级对应）：已有来源认不出、但用户**主动改选**一个认得的
   * 新来源 ⇒ payload 里要带那个新 id（不能因为「原值认不出」就把它吞掉 = 假保存）。
   */
  it('已有来源认不出 + 用户主动改选认得的来源 ⇒ payload 带**那个新 id**（不是 undefined、也不是 null）', async () => {
    vi.mocked(fetchShopBindings).mockResolvedValue({
      items: [{ ...ROW, order_source: 5 }],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const root = document.createElement('div')
    document.body.appendChild(root)
    vi.mocked(fetchOrderSources).mockResolvedValue({
      items: [SOURCE],
      total: 1,
      page: 1,
      page_size: 200,
    })
    const cleanup = await mountPddshipsettingPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.table.reloadData).toHaveBeenCalled())
    fireTool('submitEdit', renderedRows()[0]!)
    const panel = openedPanel()
    const select = panel.querySelector<HTMLSelectElement>('select#tbsoure')!
    select.value = '3'
    clickSave(panel)
    await vi.waitFor(() => expect(updateShopBinding).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(updateShopBinding).mock.calls[0]![1] as Record<string, unknown>
    expect(payload.order_source).toBe(3)
    cleanup()
  })

  it('对照组：字典正常时，用户**主动**选「不设置」照发 `null`（清空这条路没被误封）', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', renderedRows()[0]!)
    const panel = openedPanel()
    const select = panel.querySelector<HTMLSelectElement>('select#tbsoure')!
    expect(select.value).toBe('3')
    select.value = ''
    clickSave(panel)
    await vi.waitFor(() => expect(updateShopBinding).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(updateShopBinding).mock.calls[0]![1] as Record<string, unknown>
    expect(payload.order_source).toBeNull()
    cleanup()
  })
})

/* ───────────────────────── 尚未开放（D-0025 §5 / spec §7.3-P2） ───────────────────────── */

describe('尚未开放：捕获阶段拦截 → 只提示、不发请求、状态不变', () => {
  /** 把 root 挂进一个祖先里：`stopPropagation` 的判据是**事件到不了祖先**（先例 sourelist 的 MUT-3）。 */
  let outer: HTMLElement
  beforeEach(() => {
    outer = document.createElement('div')
    document.body.appendChild(outer)
  })

  it('`notOpenLabel`：三颗具名控件取自己的文案，五段设置取它所在项的说明文字', () => {
    const doc = new DOMParser().parseFromString(EVIDENCE, 'text/html')
    expect(notOpenLabel(doc.querySelector<HTMLElement>('.payBtn')!)).toBe('购买接口调用量')
    expect(notOpenLabel(doc.querySelector<HTMLElement>('.getAuth')!)).toBe('获取授权')
    expect(notOpenLabel(doc.querySelector<HTMLElement>('button[lay-filter="savebtn"]')!)).toBe(
      '保存以上设置',
    )
    expect(notOpenLabel(doc.querySelector<HTMLElement>('input[name="Xianyu_AutoDeliverSwitch"]')!)).toBe(
      '功能总开关(如果关闭，则该对接功能将全部失败)',
    )
  })

  it('「获取授权」→ 提示 + 捕获阶段收口（事件到不了祖先），零写请求', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    fireToolbar('addBtn')
    click(openedPanel().querySelector('.getAuth')!)
    expect(LAYUI.msg).toHaveBeenCalledWith('「获取授权」尚未开放', { time: 1500 })
    expect(leaked).not.toHaveBeenCalled()
    expect(createShopBinding).not.toHaveBeenCalled()
    expect(updateShopBinding).not.toHaveBeenCalled()
    outer.removeEventListener('click', leaked)
    cleanup()
  })

  it('「保存以上设置」→ 提示 + 事件收口，不发任何写请求', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    click(root.querySelector('button[lay-filter="savebtn"]')!)
    expect(LAYUI.msg).toHaveBeenCalledWith('「保存以上设置」尚未开放', { time: 1500 })
    expect(leaked).not.toHaveBeenCalled()
    expect(createShopBinding).not.toHaveBeenCalled()
    outer.removeEventListener('click', leaked)
    cleanup()
  })

  it('五段平台功能设置里的单选 → 提示 + 事件收口（不切过去 = 不假装成功）', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    const radio = root.querySelector<HTMLInputElement>('input[name="Xianyu_AutoDeliverSwitch"]')!
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    click(radio)
    expect(LAYUI.msg).toHaveBeenCalledWith(
      '「功能总开关(如果关闭，则该对接功能将全部失败)」尚未开放',
      { time: 1500 },
    )
    expect(leaked).not.toHaveBeenCalled()
    outer.removeEventListener('click', leaked)
    cleanup()
  })

  it('「点击这里购买接口调用量」→ 提示 + 事件收口（结构保留、点了不跳转）', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    click(root.querySelector('.payBtn')!)
    expect(LAYUI.msg).toHaveBeenCalledWith('「购买接口调用量」尚未开放', { time: 1500 })
    expect(leaked).not.toHaveBeenCalled()
    outer.removeEventListener('click', leaked)
    cleanup()
  })

  it('**弹层里的平台单选是真的**：点了不提示、不被收口（拦截范围只包「后端没有」的控件）', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    fireToolbar('addBtn')
    const radio = openedPanel().querySelector<HTMLElement>('[name="platforms"][value="20"]')!
    const leaked = vi.fn()
    outer.addEventListener('click', leaked)
    click(radio)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    // 面板里的点击**照常冒泡**（弹层自己的控件不归「尚未开放」管）。
    expect(leaked).toHaveBeenCalledTimes(1)
    outer.removeEventListener('click', leaked)
    cleanup()
  })

  /**
   * **这一条是「拦截集不许再长回去」的守卫**（评审修复轮 1 #1）。
   *
   * `artifactPage.ts:271-273` 的原样选择器是 `button, a.layui-btn, .layui-form-switch, [lay-event],
   * [lay-filter]`——把这几个加回 `NOT_OPEN_SELECTOR`，**四个按钮会全死**（捕获阶段先截走，
   * layui 的冒泡委托收不到），而这正是本任务重写要修掉的失效形态。没有这条用例时，
   * 那种回退会让 39 条**全绿**。
   *
   * 判据结构：真点一颗 `[lay-event]` 按钮 → ① 没有被「尚未开放」提示；② 事件照常冒泡到
   * **模拟 layui 表格委托**的冒泡监听器上，于是真 handler 被调到（`addBtn` 打开了新增弹层）。
   */
  it('**真点 `[lay-event]` 按钮不被拦**：闸门只包「尚未开放」的控件，工具栏照常走到 layui 的冒泡委托', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    // 造一颗**真**按钮：真 layui 会把它渲染进 `.layui-table-tool`（表格工具栏）那一带。
    const btn = document.createElement('button')
    btn.className = 'layui-btn layui-btn-xs'
    btn.setAttribute('lay-event', 'addBtn')
    root.appendChild(btn)
    // 模拟 **layui 表格自己**的 `*[lay-event]` 委托：它在**冒泡阶段**、且挂在比 root 更外层
    // （`artifactPage.ts:265-268` 读码：layui 对 `.layui-table-tool *[lay-event]` 自己做了委托）。
    const delegated = vi.fn((event: Event) => {
      const target = (event.target as HTMLElement).closest<HTMLElement>('[lay-event]')
      if (!target) return
      fireToolbar(target.getAttribute('lay-event')!)
    })
    outer.addEventListener('click', delegated)
    click(btn)
    // ① 闸门没碰它：没有任何「尚未开放」提示。
    expect(LAYUI.msg).not.toHaveBeenCalled()
    // ② 事件冒到了 layui 的委托 ⇒ 真 handler 跑到 ⇒ 新增弹层被打开。
    expect(delegated).toHaveBeenCalledTimes(1)
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(lastOpenConfig().title).toBe('新增一个店铺对接')
    outer.removeEventListener('click', delegated)
    cleanup()
  })

  /**
   * **同一守卫的第二种形状：行内那两颗是 `<a class="layui-btn" lay-event>`**（证据件 `html:85-86`）。
   *
   * 修复轮 2 #4 实测：**只**把 `a.layui-btn` 加回 `NOT_OPEN_SELECTOR` 时上面那条 `button` 版本的用例
   * **照样绿**（`button` ≠ `a.layui-btn`）——而行内的「修改 / 删除」恰恰是锚点，于是「只加回
   * `a.layui-btn`」这种窄回退无人守。这里真点一颗**行内修改锚点**，断言它不被拦、且事件冒到
   * layui 的行内委托上（`table.on('tool')` 真 handler 跑到 ⇒ 打开了「编辑对接」弹层）。
   */
  it('**真点行内 `<a class="layui-btn" lay-event>` 不被拦**：锚点形状的按钮也不许混进拦截集', async () => {
    const { root, cleanup } = await mount()
    outer.appendChild(root)
    // 造一颗**与证据件 `#baroperating` 逐字同形**的行内按钮（`<a class="layui-btn …" lay-event=…>`）。
    const anchor = document.createElement('a')
    anchor.className = 'layui-btn layui-btn-xs layui-btn-warm'
    anchor.setAttribute('lay-event', 'submitEdit')
    anchor.textContent = '修改'
    root.appendChild(anchor)
    // 模拟 layui 表格对行内按钮的委托（同样是**冒泡**阶段、挂在比 root 更外层）。
    const delegated = vi.fn((event: Event) => {
      const target = (event.target as HTMLElement).closest<HTMLElement>('[lay-event]')
      if (!target?.closest('a.layui-btn')) return
      fireTool('submitEdit', renderedRows()[0]!)
    })
    outer.addEventListener('click', delegated)
    click(anchor)
    expect(LAYUI.msg).not.toHaveBeenCalled()
    expect(delegated).toHaveBeenCalledTimes(1)
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(lastOpenConfig().title).toBe('编辑对接')
    outer.removeEventListener('click', delegated)
    cleanup()
  })
})
