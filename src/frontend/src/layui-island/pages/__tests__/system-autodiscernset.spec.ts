// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RecognitionItemRow } from '@/api/recognition'

/**
 * 【录单自动识别设置】island 页（`pages/system-autodiscernset.ts`）的契约测试。
 *
 * 覆盖计划 T7 Step 1 的四条硬行为：**14 行渲染 + 列定义逐条**、**点「修改关键词」真的开弹层且预填对**、
 * **改值保存 → PATCH 载荷正确 + 成功后重载并提示**、**失败原样显示后端 msg 且不关弹层**；
 * 另加第一列蓝字那条路（对标有两条开弹层的路）与证据件对齐（`lay-filter="dis"` / `maxlength=25`）。
 *
 * 手法照 `system-sourelist.spec.ts`：`vi.mock` 掉 `@/layui/island` 的 `bootstrapIsland`
 * （jsdom 里加载不了真 layui）与 `@/api/recognition`，只断言**入参**；
 * 证据件用**真实文件**（`public/replica/system-autodiscernset.html`），不另造一份标记——
 * 否则「弹层里到底有没有那个 `lay-filter="dis"` 的表单 / `maxlength=25`」这类断言会与真实产物脱钩。
 *
 * ⚠️ `vi.mock` 会被提升，`import` 写在它之后。
 */
vi.mock('@/api/recognition', () => ({
  fetchRecognitionSettings: vi.fn(),
  updateRecognitionKeywords: vi.fn(),
}))

/**
 * layui 的**可控替身**。`form.val` 刻意实现成「按 `<form lay-filter>` 的作用域找输入」——
 * 这正是「先预填、再 open」那条硬要求的判据：一旦把顺序写反（先 open 把 `.change` 搬出
 * `<form>`，再 `form.val`），替身就找不到输入，预填断言变红。
 *
 * ⚠️ 真 layui 的 `form` 提交体（`public/layui/layui.js` 的 `t.prototype.submit`）是从
 * `a.parents(form).eq(0)` 取表单再 `getValue` 的 ⇒ 节点被 `layer.open` 搬出 `<form>` 后
 * `data.field` 是 `{}`。所以本页的保存**必须**从弹层 DOM 读值（`[name="key"]`），
 * 替身不提供 `data.field` 这条捷径。
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
     * `layer.open({type:1, content})` 要的是 **jQuery 对象**（`config-replica.ts` 的
     * `openPanelLayer`；传裸 `HTMLElement` 真 layui 会抛 `TypeError: r.parents is not a function`）
     * ——替身只包一层，断言用的 `content.el` 就是那个节点。
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

import { fetchRecognitionSettings, updateRecognitionKeywords } from '@/api/recognition'
import { BizError } from '@/api/errors'
import {
  buildCols,
  COLS,
  dialogOptions,
  FORM_FILTER,
  mountRecognitionPage,
  PANEL_CLASS,
  PANEL_SELECTOR,
  TABLE_ELEM,
  TABLE_ID,
  TITLE,
  toRow,
  type RecognitionRow,
} from '../system-autodiscernset'

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** 证据件原文（`public/replica/system-autodiscernset.html`；4 层上去 = `src/frontend`）。 */
const EVIDENCE = readFileSync(
  path.resolve(HERE, '../../../../public/replica/system-autodiscernset.html'),
  'utf8',
)

/** 14 行真实形状的数据（`item_code` / `name` / `keywords`，顺序即页面顺序，spec §1.2）。 */
const ROWS: RecognitionItemRow[] = [
  { item_code: 'game_server_area', name: '游戏区服', keywords: '服务器,区服,平台,大区,b服,官服,卓还是,果还' },
  { item_code: 'server', name: '服务器', keywords: '游戏服,服务器' },
  { item_code: 'game_account', name: '游戏账号', keywords: '帐号,战网,账号,通行证' },
  { item_code: 'sub_account', name: '子副账号', keywords: '子账号,分账号,子战网,子帐号,ID' },
  { item_code: 'role_name', name: '角色名', keywords: '角色,名字,名称,昵称' },
  { item_code: 'current_info_1', name: '当前信息1', keywords: '装等,装备,等级,当前,排位,铭文,符文' },
  { item_code: 'current_info_2', name: '当前信息2', keywords: '盟约,巅峰,天赋,英雄' },
  { item_code: 'project_text', name: '代练内容', keywords: '项目,内容,业务,类型,要求,需求,代,任务,标题' },
  { item_code: 'price', name: '接单价格', keywords: '价格,接单价,金额' },
  { item_code: 'order_source', name: '订单来源', keywords: '来源,店铺' },
  { item_code: 'source_order_no', name: '来源单号', keywords: '订单编号,单号,订单号,定单编号' },
  { item_code: 'phone', name: '联系电话', keywords: '手机,电话,联系方式' },
  { item_code: 'qq', name: 'QQ', keywords: 'qq,微信' },
  { item_code: 'ww', name: '旺旺', keywords: '旺旺,淘宝旺旺' },
]

const PRICE_ROW = ROWS[8]!

function textResponse(text: string): Response {
  return { ok: true, status: 200, text: async () => text } as unknown as Response
}

/** 挂载整页：证据件走全局 fetch（真文件），列表走 `@/api/recognition` 的替身。 */
async function mount(rows: RecognitionItemRow[] = ROWS): Promise<{
  root: HTMLElement
  cleanup: () => void
  cfg: Record<string, unknown>
}> {
  vi.mocked(fetchRecognitionSettings).mockResolvedValue({ items: rows })
  const root = document.createElement('div')
  document.body.appendChild(root)
  const cleanup = await mountRecognitionPage(root, { onNavigate: vi.fn() })
  const cfg = LAYUI.table.render.mock.calls[0]![0] as Record<string, unknown>
  return { root, cleanup, cfg }
}

/** 点一次行内事件（layui 的 `table.on('tool(tableList)')`）。 */
function fireTool(event: string, row: RecognitionRow = toRow(PRICE_ROW)): void {
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

function panelInput(panel: HTMLElement, name: string): HTMLInputElement {
  const el = panel.querySelector<HTMLInputElement>(`[name="${name}"]`)
  expect(el, `弹层里没有 [name="${name}"]`).toBeTruthy()
  return el!
}

beforeEach(() => {
  LAYUI.table.render.mockClear()
  LAYUI.table.reloadData.mockClear()
  LAYUI.msg.mockClear()
  // ⚠️ 用 `mockReset`（不是 `mockClear`）：下面有条用例会临时换掉 `open` 的实现
  //    （模拟 layui 把节点搬进 `.layui-layer-content`），`mockClear` 只清调用记录、
  //    **保留实现** ⇒ 会把那个「搬走」行为泄漏给后面的用例。
  LAYUI.open.mockReset()
  LAYUI.open.mockReturnValue(1)
  LAYUI.layer.close.mockClear()
  LAYUI.form.render.mockClear()
  LAYUI.form.val.mockClear()
  LAYUI.formVals.length = 0
  LAYUI.tableHandlers.clear()
  LAYUI.formHandlers.clear()
  vi.mocked(fetchRecognitionSettings).mockReset()
  vi.mocked(updateRecognitionKeywords).mockReset()
  vi.mocked(updateRecognitionKeywords).mockResolvedValue(PRICE_ROW)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      if (String(url).includes('system-autodiscernset.html')) return textResponse(EVIDENCE)
      throw new Error(`未预期的 fetch：${String(url)}`)
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

/* ───────────────────────── 常量与证据件对齐 ───────────────────────── */

describe('常量与证据件：外观的唯一出处是 `system-autodiscernset.html`', () => {
  it('页 `<title>` 与表格/弹层的三个标识逐字取值', () => {
    expect(TITLE).toBe('录单自动识别设置')
    expect(TABLE_ELEM).toBe('#tableList')
    expect(TABLE_ID).toBe('tableList')
    expect(FORM_FILTER).toBe('dis')
    expect(PANEL_SELECTOR).toBe('.change')
    // ⛔ 类名与选择器必须分成两个常量：`class="${PANEL_SELECTOR}"` 会造出 `class=".change"`。
    expect(PANEL_CLASS).toBe('change')
  })

  it('证据件里真有这些标记（常量不是凭记忆写的）', () => {
    expect(EVIDENCE).toContain('lay-filter="dis"')
    expect(EVIDENCE).toContain('lay-filter="tableList"')
    expect(EVIDENCE).toContain('name="key"')
    expect(EVIDENCE).toContain('maxlength="25"')
    expect(EVIDENCE).toContain(
      'placeholder="请设定能识别订单内容中的项目名称关键词，例如：账号 密码，每个关键词之间用逗号或空格分隔"',
    )
    expect(EVIDENCE).toContain('id="baroperating"')
    expect(EVIDENCE).toContain('layui-btn-xs layui-btn-warm')
    expect(EVIDENCE).toContain('>修改关键词</a>')
    expect(EVIDENCE).toContain('lay-filter="savebtn"')
    // `#disID` 是隐藏输入，装 item_code（对标装它的数字 disId）。
    expect(EVIDENCE).toContain('id="disID"')
    expect(EVIDENCE).toContain('name="disID"')
    // 对标这页**没有**表格工具栏（`.layui-btn-container` 数为 0）⇒ 证据件里没有 `layui-btn-container`。
    expect(EVIDENCE).not.toContain('layui-btn-container')
  })
})

/* ───────────────────────── 列定义（逐条照对标 JS） ───────────────────────── */

describe('buildCols：三列逐条等于对标取值', () => {
  const cols = buildCols()
  const byField = (field: string): Record<string, unknown> => cols.find((c) => c.field === field)!

  it('恰好三列，标题逐字', () => {
    expect(cols).toHaveLength(3)
    expect(cols.map((c) => c.title)).toEqual([
      '识别项名称',
      '订单内容中每一行可以识别的关键词',
      '操作',
    ])
  })

  it("第一列：`fixed:'left'` / width 100 / align left / `unresize`", () => {
    const col = byField('typeName')
    expect(col.fixed).toBe('left')
    expect(col.width).toBe(100)
    expect(col.align).toBe('left')
    expect(col.unresize).toBe(true)
  })

  it('第二列：align left / `sort:false` 照写 / `unresize`（宽度不写 = 自适应）', () => {
    const col = byField('disKey')
    expect(col.sort).toBe(false)
    expect(col.align).toBe('left')
    expect(col.unresize).toBe(true)
    expect(col.width).toBeUndefined()
  })

  it("操作列：`fixed:''`（不固定，照抄） / width 120 / `toolbar:'#baroperating'` / `unresize`", () => {
    expect(cols[2]!.title).toBe('操作')
    expect(cols[2]!.fixed).toBe('')
    expect(cols[2]!.width).toBe(120)
    expect(cols[2]!.toolbar).toBe('#baroperating')
    expect(cols[2]!.unresize).toBe(true)
    expect(cols[2]!.field).toBeUndefined()
    // 行内按钮模板在**证据件**里（不在这里另拼一份 HTML）。
    expect(EVIDENCE).toContain(`<script type="text/html" id="baroperating">`)
  })

  it('骨架与 `COLS` 是同一份出处（不会声明漂移）', () => {
    expect(cols.map((c) => c.field)).toEqual(COLS.map((c) => c.field))
    expect(cols.map((c) => c.title)).toEqual(COLS.map((c) => c.title))
    expect(cols.map((c) => c.width)).toEqual(COLS.map((c) => c.width))
  })

  it('第一列蓝字链接：`class="editrow"` + 三个 `data-*`（`data-disid` 装**我们的 item_code**）+ 蓝字 + `title`', () => {
    const cell = byField('typeName').templet as (r: RecognitionRow) => string
    expect(cell(toRow(PRICE_ROW))).toBe(
      '<a href="javascript:void(0);" class="editrow" data-disid="price" data-typename="接单价格" ' +
        'data-diskey="价格,接单价,金额" style="color:#1E9FFF" title="接单价格">接单价格</a>',
    )
  })

  it('关键词列也是 `<a>` + 空 `class=""`（照对标 templet）', () => {
    const cell = byField('disKey').templet as (r: RecognitionRow) => string
    expect(cell(toRow(PRICE_ROW))).toBe(
      '<a href="javascript:void(0);" class="" title="价格,接单价,金额">价格,接单价,金额</a>',
    )
  })

  /**
   * **XSS（I-4 同族）**：`templet` 的返回值被 layui **当 HTML 插入**，而 `name` 与 `keywords`
   * 都是可编辑文本（`keywords` 上限 255）⇒ 必须转义。
   */
  it('第一列的名称被转义（`title` / `data-*` 属性上下文不能被闭合）', () => {
    const cell = byField('typeName').templet as (r: RecognitionRow) => string
    const html = cell({ itemCode: 'price', typeName: '"><img>', disKey: 'x' })
    expect(html).not.toContain('<img')
    expect(html).toContain('data-typename="&quot;&gt;&lt;img&gt;"')
    expect(html).toContain('title="&quot;&gt;&lt;img&gt;"')
  })

  it('关键词列的整段 payload 被转义', () => {
    const cell = byField('disKey').templet as (r: RecognitionRow) => string
    const html = cell({ itemCode: 'price', typeName: '接单价格', disKey: '<img src=x onerror=alert(1)>' })
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
  })
})

/* ───────────────────────── 弹层参数 ───────────────────────── */

describe('dialogOptions：逐条照对标的 layer.open 参数', () => {
  it('标题 = 「修改」+ 名称 + 「的识别关键词」，其余六项照抄', () => {
    expect(dialogOptions(toRow(PRICE_ROW), 'CONTENT')).toEqual({
      title: '修改接单价格的识别关键词',
      shade: [0.3, '#333'],
      shadeClose: true,
      skin: 'layui-layer-lan',
      anim: 8,
      area: ['80%', '180px'],
      content: 'CONTENT',
    })
  })
})

/* ───────────────────────── 表格声明 + 14 行渲染 ───────────────────────── */

describe('挂载：表格声明与 14 行真数据', () => {
  it('table.render：无 toolbar / `defaultToolbar: []` / `page:false` / height / cellMinWidth / 无 url', async () => {
    const { cleanup, cfg } = await mount()
    expect(cfg.elem).toBe('#tableList')
    expect(cfg.id).toBe(TABLE_ID)
    // ⛔ 对标**没有**表格工具栏 ⇒ 不传 `toolbar`；`defaultToolbar` 是**空数组**（不是 `['filter']`）。
    expect(cfg.toolbar).toBeUndefined()
    expect(cfg.defaultToolbar).toEqual([])
    expect(cfg.page).toBe(false)
    expect(cfg.height).toBe('full')
    expect(cfg.cellMinWidth).toBe(100)
    // 数据由页面自己取（`fetchRecognitionSettings` → `reloadData`），**不给** layui 传 `url`。
    expect(cfg.url).toBeUndefined()
    expect(cfg.data).toEqual([])
    expect(cfg.text).toEqual({ none: '暂无数据' })
    expect((cfg.cols as unknown[][])[0]).toHaveLength(3)
    cleanup()
  })

  it('14 行一行不少地交给 `table.reloadData(id, {data})`（首参是 id、不带 `#`，E-0061）', async () => {
    const { cleanup } = await mount()
    expect(fetchRecognitionSettings).toHaveBeenCalledTimes(1)
    const reload = LAYUI.table.reloadData.mock.calls[0]!
    expect(reload[0]).toBe(TABLE_ID)
    expect(reload[0]).not.toBe(`#${TABLE_ID}`)
    const payload = reload[1] as { data: RecognitionRow[] }
    expect(payload.data).toHaveLength(14)
    // 行映射用**字面值**钉住首尾两行（E-0024：不只数长度）。
    expect(payload.data[0]).toEqual({
      itemCode: 'game_server_area',
      typeName: '游戏区服',
      disKey: '服务器,区服,平台,大区,b服,官服,卓还是,果还',
    })
    expect(payload.data[13]).toEqual({ itemCode: 'ww', typeName: '旺旺', disKey: '旺旺,淘宝旺旺' })
    cleanup()
  })

  it('取数失败 → `msg(e.message)`，不抛、不渲染假数据', async () => {
    vi.mocked(fetchRecognitionSettings).mockRejectedValue(new BizError(1002, '分页参数非法'))
    const root = document.createElement('div')
    document.body.appendChild(root)
    const cleanup = await mountRecognitionPage(root, { onNavigate: vi.fn() })
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('分页参数非法', { time: 1500 }))
    expect(LAYUI.table.reloadData).not.toHaveBeenCalled()
    cleanup()
  })

  it('`document.title` 逐字等于对标 `<title>`', async () => {
    const { cleanup } = await mount()
    expect(document.title).toBe('录单自动识别设置')
    cleanup()
  })
})

/* ───────────────────────── 开弹层的两条路 ───────────────────────── */

describe('开弹层：行内「修改关键词」与第一列蓝字两条路', () => {
  it('行内按钮 → 弹层打开，标题含名称，关键词与 `disID` 预填对', async () => {
    const { root, cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const config = lastOpenConfig()
    expect(config.type).toBe(1)
    expect(config.title).toBe('修改接单价格的识别关键词')
    expect(config.shade).toEqual([0.3, '#333'])
    expect(config.shadeClose).toBe(true)
    expect(config.skin).toBe('layui-layer-lan')
    expect(config.anim).toBe(8)
    expect(config.area).toEqual(['80%', '180px'])
    // ⚠️ `lay-filter` 必须是 `dis`（证据件 L86）——写成 `'form'` 时预填**静默失效**。
    expect(LAYUI.form.val).toHaveBeenCalledWith('dis', {
      key: '价格,接单价,金额',
      disID: 'price',
    })
    const panel = openedPanel()
    expect(panelInput(panel, 'key').value).toBe('价格,接单价,金额')
    // ⚠️ `#disID` 是 `<form>` 里的隐藏输入、在 `.change` **外面**（证据件 L110）⇒ 它**不随**
    //    弹层节点被搬走，判据要打在表单那一层，不能在弹层节点里找它。
    expect(root.querySelector<HTMLInputElement>('#disID')!.value).toBe('price')
    cleanup()
  })

  it('第一列蓝字（`.editrow` 的委托点击）→ 同样开弹层并预填（值来自 `data-*`）', async () => {
    const { root, cleanup } = await mount()
    const cell = buildCols().find((c) => c.field === 'typeName')!.templet as (
      r: RecognitionRow,
    ) => string
    const host = document.createElement('div')
    host.innerHTML = cell(toRow(PRICE_ROW))
    root.appendChild(host.firstElementChild!)
    const link = root.querySelector<HTMLElement>('.editrow')
    expect(link, '第一列没渲染出 `.editrow`').toBeTruthy()
    link!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    expect(lastOpenConfig().title).toBe('修改接单价格的识别关键词')
    expect(LAYUI.form.val).toHaveBeenCalledWith('dis', {
      key: '价格,接单价,金额',
      disID: 'price',
    })
    cleanup()
  })

  it('节点被表格重绘整个摘掉后，第二次打开从快照补回干净节点（不静默打不开）', async () => {
    const { root, cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    const stolen = openedPanel()
    // 模拟 layui 重绘那一带：节点整个从文档里消失（不是搬进 `.layui-layer-content`）。
    stolen.remove()
    LAYUI.open.mockClear()
    LAYUI.formVals.length = 0
    fireTool('submitEdit', toRow(ROWS[13]!))
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    const reopened = openedPanel()
    expect(reopened).not.toBe(stolen)
    expect(root.querySelector('form')!.contains(reopened)).toBe(true)
    expect(LAYUI.formVals[0]).toEqual({ key: '旺旺,淘宝旺旺', disID: 'ww' })
    cleanup()
  })

  it('**先预填、再 open**：`layer.open` 拿到的是**仍在 `<form>` 里**的节点，且是三步里的最后一步', async () => {
    const { root, cleanup } = await mount()
    // 真 layui 的 `layer.open({type:1, content: jq})` 会把节点**搬出 `<form>`**；替身照此实现。
    LAYUI.open.mockImplementation(() => {
      const panel = document.querySelector<HTMLElement>('.change')
      const holder = document.createElement('div')
      holder.className = 'layui-layer-content'
      document.body.appendChild(holder)
      if (panel) holder.appendChild(panel)
      return 1
    })
    fireTool('submitEdit', toRow(PRICE_ROW))
    expect(LAYUI.open).toHaveBeenCalledTimes(1)
    // 顺序判据：`form.val` / `form.render` 都发生在 `layer.open` **之前**。
    expect(LAYUI.form.val.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.open.mock.invocationCallOrder[0]!,
    )
    expect(LAYUI.form.render.mock.invocationCallOrder[0]).toBeLessThan(
      LAYUI.open.mock.invocationCallOrder[0]!,
    )
    expect(root.querySelector('form')!.contains(openedPanel())).toBe(false)
    cleanup()
  })
})

/* ───────────────────────── 保存 ───────────────────────── */

describe('保存：从弹层 DOM 读值 → PATCH → 关弹层 + 重载 + 提示', () => {
  it('载荷 = `updateRecognitionKeywords(item_code, 输入框里的关键词)`，成功后关弹层 + 重载 + 「保存成功」', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    const panel = openedPanel()
    // 改值：**从 DOM 读**是硬要求（`data.field` 在节点被搬出 `<form>` 后是 `{}`）。
    panelInput(panel, 'key').value = '价格,总价'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(updateRecognitionKeywords).toHaveBeenCalledTimes(1))
    expect(updateRecognitionKeywords).toHaveBeenCalledWith('price', '价格,总价')
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    await vi.waitFor(() => expect(fetchRecognitionSettings).toHaveBeenCalledTimes(2))
    expect(LAYUI.msg).toHaveBeenCalledWith('保存成功', { time: 1500 })
    cleanup()
  })

  it('清空关键词（空串）也照发——清空是合法覆盖，不是「没填」', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    panelInput(openedPanel(), 'key').value = ''
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(updateRecognitionKeywords).toHaveBeenCalledTimes(1))
    expect(updateRecognitionKeywords).toHaveBeenCalledWith('price', '')
    cleanup()
  })

  it('后端拒绝 → 原样显示 `e.message`，**不关弹层**、不发「保存成功」', async () => {
    vi.mocked(updateRecognitionKeywords).mockRejectedValue(new BizError(1000, '关键词过长'))
    const { cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    panelInput(openedPanel(), 'key').value = '长'.repeat(30)
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(LAYUI.msg).toHaveBeenCalledWith('关键词过长', { time: 1500 }))
    expect(LAYUI.msg).not.toHaveBeenCalledWith('保存成功', { time: 1500 })
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    // 失败后不重载（列表不该因为一次失败而闪一下）。
    expect(fetchRecognitionSettings).toHaveBeenCalledTimes(1)
    cleanup()
  })

  it('第二次打开编辑的是**新那一行**（`item_code` 由闭包记住，不依赖 `#disID` 的当前值）', async () => {
    const { cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    panelInput(openedPanel(), 'key').value = '价格,总价'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(updateRecognitionKeywords).toHaveBeenCalledTimes(1))
    fireTool('submitEdit', toRow(ROWS[13]!))
    panelInput(openedPanel(), 'key').value = '旺旺'
    LAYUI.formHandlers.get('submit(savebtn)')!()
    await vi.waitFor(() => expect(updateRecognitionKeywords).toHaveBeenCalledTimes(2))
    expect(updateRecognitionKeywords).toHaveBeenLastCalledWith('ww', '旺旺')
    cleanup()
  })
})

/* ───────────────────────── 卸载 ───────────────────────── */

describe('卸载', () => {
  it('关掉弹层并清空 root', async () => {
    const { root, cleanup } = await mount()
    fireTool('submitEdit', toRow(PRICE_ROW))
    cleanup()
    expect(LAYUI.layer.close).toHaveBeenCalledWith(1)
    expect(root.innerHTML).toBe('')
  })
})
