// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 【系统用户实名认证记录与配置】复刻页的**门禁**。
 *
 * ## 为什么必须有「驱动真交互」的用例（`AGENTS.md` §5-G / E-0061 / E-0067）
 *
 * 本页 2026-10-10（`feat/realname` 的 T9）**脱离了 `artifactPage`**：那个共享渲染器在**捕获阶段**
 * 把所有 `[lay-event]` / `button` 的点击收口成「尚未开放」并 `stopPropagation`
 * （`artifactPage.ts:282-298`）⇒ 在它下面接 `table.on('toolbar(…)')` 是死路（本页工具栏那三颗
 * 按钮正是 `lay-event="refresh" / "settingBtn" / "buyBtn"`）。代价：
 * `typecheck` 看不见 DOM 选择器对不对、`lint` 看不见类名里多了个点、`parity` 在空表时看不到表体
 * ⇒ **本文件是唯一防线**，而「点了没反应」是**静默**的。
 *
 * ## 三条 island 陷阱（§5-G）在本文件里各有判据
 *
 * ① 类名与选择器分两个常量 —— 页里是 `TABLE_ELEM`（`'#tableList'`）与 `TABLE_ID`（`'tableList'`），
 *    本文件用「`table.reload` 的第一个实参必须是 `TABLE_ID`」把它钉住（传 `#id` 是静默 no-op）；
 * ② 以 `(` 开头的语句前置分号 —— 页代码风格，由 `pnpm lint` 守；
 * ③ **先 `await bootstrapIsland(...)` 再塞标记**、而 `initIslandElements(layui)` 留在塞标记**之后**
 *    —— 本文件用 `LAYUI.state` 两个快照把它钉住。
 *
 * ## 判据只打在**真实出口**上
 *
 * 列表取数的形状打在 `table.render` / `table.reload` 真正收到的那份配置上（= 真人操作时
 * layui 会拿去发请求的那一份）；配置弹层的四项打在**服务端返回值经过回填之后的 DOM** 上
 * （⛔ 不钉写死在代码里的常量）；购买按钮打在 `layer.msg` 的文案上。
 */

/** 证据件原文（**只读**）：本页的标记层就是它 ⇒ 断言「弹层那 4 项原文在不在」必须读它本身。 */
const EVIDENCE = fs.readFileSync(
  path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../public/replica/user-rnamecertlogandsetting.html',
  ),
  'utf8',
)

/**
 * layui 的**可控替身**。真 layui 在 jsdom 里加载不了（先例：`order-tradingorderactionloglist.spec.ts`）。
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
      open: vi.fn(() => 7),
      close: vi.fn(),
      confirm: vi.fn(() => 3),
      photos: vi.fn(),
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

// 证据件走替身（真实现是运行期 `fetch('replica/…')`）—— 但喂的是**真文件的内容**，
// 所以「弹层那 4 项的问句 / 选项原文」这类断言仍然是对着证据件本身做的。
vi.mock('@/layui-island/config-replica', () => ({
  loadConfigArtifact: async () => EVIDENCE,
}))

// 取数层走替身：本文件断言的是**页面交给取数层的那份参数**（两段式的形状），
// 不是 HTTP 细节（那由 `api/__tests__/realname.spec.ts` 钉 URL 与查询串）。
vi.mock('@/api/realname', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/realname')>()
  return {
    ...actual,
    listRealNameLogs: vi.fn(),
    getRealNameSetting: vi.fn(),
    updateRealNameSetting: vi.fn(),
    getRealNameQuota: vi.fn(),
    // T3（spec §2.4）：配置实名资源包弹窗的两条取数。
    listRealNamePackages: vi.fn(),
    createRealNamePackage: vi.fn(),
  }
})

import {
  buildRealNameLogParams,
  buyButtonLabel,
  createRealNamePackage,
  getRealNameQuota,
  getRealNameSetting,
  listRealNamePackages,
  updateRealNameSetting,
  type RealNameLogParams,
  type RealNameLogRow,
  type RealNameSetting,
} from '@/api/realname'
import page, {
  buildCols,
  CERT_IMAGE_CLASS,
  CERT_IMAGE_EMPTY_TEXT,
  CERT_IMAGE_NOTICE,
  CERT_IMAGE_NOTICE_BUTTONS,
  COLS_FOR_TEST,
  defaultRealNameDateRange,
  DEFAULT_TOOLBAR,
  ID_CARD_TITLE,
  PAGE_LIMIT,
  PAGE_LIMITS,
  readSettingForm,
  TABLE_ELEM,
  TABLE_FILTER,
  TABLE_ID,
  TABLE_OPTIONS,
  TOOLBAR_ELEM,
  toLogParams,
  toRow,
  PKG_CREATED_TEXT,
  PKG_INPUT_NAMES,
  PKG_ROW_CLASS,
  QUOTA_EXHAUSTED_TEXT,
  ABNORMAL_AGE_TIPS,
  type RealNameCol,
  type Row,
} from '../user-rnamecertlogandsetting'

/** 合成夹具（PII 纪律：账号 / 姓名 / 证件号都是**合成值**，只仿形状，见 `AGENTS.md` §5-C）。 */
const API_ROW: RealNameLogRow = {
  id: 11,
  user_name: 'kefu_jia',
  name: '测试姓名甲',
  // ⚠️ 这一格**本来就是脱敏值**（后端 `_mask_pii`，前 3 后 2）——夹具也照脱敏形状造。
  id_card_no: '110***********12',
  gender: '男',
  age: 17,
  cert_method: 'basic',
  result: 'pass',
  applied_at: '2026-10-10T09:15:00+08:00',
  certified_at: '2026-10-10T09:15:03+08:00',
  // ⚠️ 本批 `cert_image` **恒空**（spec §13-2-4）——夹具照实造空串。
  cert_image: '',
}

/** 服务端的四项配置（**刻意不是代码里的默认值**：三个 `true` + 两个非 0，好让回填真的被判据驱动）。 */
const SETTING: RealNameSetting = {
  enabled: true,
  block_claim_without_cert: true,
  require_cert_for_settlement: false,
  min_age: 18,
  max_age: 45,
}

/** 服务端回的余量（**非 0**，否则「余量写进了按钮文案」这条会与证据件的字面量 `0` 撞车）。 */
const REMAINING = 37

/**
 * 余量那一发请求的**手动闸门**。
 *
 * ⚠️ 为什么需要它：余量请求是页面挂载期发出去的，若它在 `mountPage()` 把替身工具栏塞进 DOM
 * **之前**就 resolve 了，`renderBuyLabel` 会找不到按钮 ⇒ 那条用例变成**跟时序赛跑**
 * （`flush()` 里塞进去的假按钮救不了已经跑完的回调）。这里把它的 resolve 交给我们自己，
 * `mountPage()` 塞完标记再放行。
 */
type QuotaGate = {
  resolve: (v: { remaining: number | null }) => void
  reject: (e: Error) => void
}
let quotaGate: QuotaGate | null = null

/** 建一个新的闸门并把余量替身挂在它上面（`beforeEach` 与需要改读数的用例都调它）。 */
function armQuotaGate(): QuotaGate {
  let resolve!: (v: { remaining: number | null }) => void
  let reject!: (e: Error) => void
  const promise = new Promise<{ remaining: number | null }>((res, rej) => {
    resolve = res
    reject = rej
  })
  quotaGate = { resolve, reject }
  vi.mocked(getRealNameQuota).mockReturnValue(promise)
  return quotaGate
}

/** 放行余量那一发（默认读数 = `REMAINING`）。 */
function releaseQuota(remaining: number | null = REMAINING): void {
  expect(quotaGate, '余量闸门没建（`armQuotaGate()` 没被调）').not.toBeNull()
  quotaGate!.resolve({ remaining })
  quotaGate = null
}

type RenderOpts = Record<string, unknown> & {
  where?: Record<string, unknown>
  cols?: RealNameCol[][]
  parseData?: (r: unknown) => { code: number; msg: string; count: number; data: Row[] }
  request?: unknown
  toolbar?: unknown
  defaultToolbar?: unknown
  page?: unknown
}

/**
 * 余量那一发的结局（挂载期放行）：成功写读数 / 指定一个其它读数 / 直接失败。
 * ⚠️ 默认值是 `REMAINING`（**非 0**）——用 0 会让「余量写进了按钮文案」这条与证据件的
 * 字面量 `0` 撞车、失去判别力。
 */
type QuotaOutcome = { remaining: number | null } | { reject: string } | undefined

/** 挂载整页。`root` 必须**在 document 里**——陷阱 ③ 的判据要查它。 */
async function mountPage(quota: QuotaOutcome = { remaining: REMAINING }): Promise<HTMLElement> {
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, { onNavigate: vi.fn() })
  // ⚠️ 替身不渲染证据件里的 `#toolbarDemo` 模板（真 layui 会把它搬进 `.layui-table-tool`
  //    ⇒ 三颗真按钮就在页面上）。补一份**真按钮**，让「余量写进按钮文案」走真的
  //    `textContent` 赋值。⚠️ **必须在 `mount` 之后**：页面第一件事是 `root.innerHTML = 证据件`，
  //    挂载前塞进去的节点会被整块替换掉（实测：挂载前塞 ⇒ `querySelector('[lay-event="buyBtn"]')`
  //    回 null）。
  root.insertAdjacentHTML(
    'beforeend',
    '<div class="layui-table-tool">' +
      '<button class="layui-btn layui-btn-xs layui-btn-danger" lay-event="refresh" type="button">刷新列表</button>' +
      '<button class="layui-btn layui-btn-sm layui-btn-normal" lay-event="settingBtn" type="button">配置实名认证</button>' +
      '<button class="layui-btn layui-btn-sm" lay-event="buyBtn" type="button">购买实名认证套餐包(当前余量0次)</button>' +
      '</div>',
  )
  // 替身工具栏就位**之后**才放行余量那一发（见 `armQuotaGate` 的理由）。
  if (quota !== undefined && 'reject' in quota) {
    quotaGate!.reject(new Error(quota.reject))
    quotaGate = null
  } else {
    releaseQuota(quota === undefined ? REMAINING : quota.remaining)
  }
  return root
}

/** `table.render` 收到的、`elem` 匹配的那一份配置（**真正交给 layui 的出口**）。 */
function renderOpts(): RenderOpts {
  const calls = LAYUI.table.render.mock.calls as unknown as [{ elem?: string }][]
  const call = calls.find((opts) => opts[0]?.elem === TABLE_ELEM)
  expect(call, `table.render 没有渲染 ${TABLE_ELEM}`).toBeTruthy()
  return call![0] as RenderOpts
}

/**
 * 取第 `index` 次 `layer.confirm` 的**实参**（`[文案, 选项, yes, no]`）。
 *
 * ⚠️ 必须走 `as unknown as`：`vi.fn(() => 3)` 的 `mock.calls` 是**空元组**类型
 * （`[]`），直接索引会报 TS2493（先例：`order-tradingorderactionloglist.spec.ts` 的
 * `lastReload()` 用同一种写法收窄）。⛔ 不要为了绕过它把替身改成 `vi.fn((...a: unknown[]) => 3)`
 * ——那会把 `layer.confirm` 的签名放宽成一个「什么参数都收」的函数，反而丢掉类型信息。
 */
function confirmCall(index = 0): [string, { btn: string[] }, () => void, () => void] {
  const calls = LAYUI.layer.confirm.mock.calls as unknown as [
    string,
    { btn: string[] },
    () => void,
    () => void,
  ][]
  const call = calls[index]
  expect(call, `layer.confirm 第 ${index + 1} 次没被调用`).toBeTruthy()
  return call!
}

/** 最近一次 `table.reload` 的入参。⚠️ 第一个实参必须是实例 id（陷阱 ①）。 */
function lastReload(): { where: Record<string, unknown>; page: { curr?: number } } {
  const calls = LAYUI.table.reload.mock.calls
  expect(calls.length, 'table.reload 没被调用').toBeGreaterThan(0)
  const call = calls[calls.length - 1] as unknown[]
  expect(call[0], '`table.reload` 的第一个实参必须是实例 id（传 `#id` 是静默 no-op）').toBe(
    TABLE_ID,
  )
  return call[1] as { where: Record<string, unknown>; page: { curr?: number } }
}

/** 触发 layui 登记的那个真回调（= 真人点击时 layui 会调的函数）。 */
function fireTableEvent(name: string, obj: Record<string, unknown>): void {
  const handler = LAYUI.tableHandlers.get(`${name}(${TABLE_FILTER})`)
  expect(handler, `页面没有注册 ${name}(${TABLE_FILTER})`).toBeTypeOf('function')
  handler!(obj)
}

function fireFormSubmit(filter: string): void {
  const handler = LAYUI.formHandlers.get(`submit(${filter})`)
  expect(handler, `页面没有注册 submit(${filter})`).toBeTypeOf('function')
  handler!()
}

/** 让 `await` 链跑完（替身都是已 resolve 的 promise，两跳足够）。 */
const flush = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

/** 某列（按 `title`）。 */
function colByTitle(cols: RealNameCol[], title: string): RealNameCol {
  const col = cols.find((c) => c.title === title)
  expect(col, `没有标题为「${title}」的列`).toBeTruthy()
  return col!
}

/** 把某列的 `templet` **真产物**变成 DOM 节点（这样「写哪个属性」也被驱动到）。 */
function renderCell(col: RealNameCol, row: Row): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = (col.templet as (d: Row) => string)(row)
  return host
}

/**
 * 源码里的**代码**部分（剥掉 `//` 与 `/* … *\/` 注释）。
 *
 * ⚠️ 为什么必须剥：本页的注释里**故意**记着对标那条外链与那个隐藏域的**名字**（那是排障信息，
 * 说明「我们为什么不照它做」）——不剥就会把「文档里提了一句」判成「代码里用了它」（假红）。
 * ⚠️ 边界（如实登记）：这是**朴素**的剥法（不解析字符串字面量），够本文件的用途——
 * 本页的字符串里没有 `//` 或 `/*` 这种形状；真要更严就得走真正的解析器。
 */
function codeOnly(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

/** 驱动列表响应（layui 收到响应后调的那一次 `parseData`）。 */
function driveResponse(items: RealNameLogRow[] = [API_ROW]): void {
  const opts = renderOpts()
  expect(opts.parseData).toBeTypeOf('function')
  opts.parseData!({
    code: 0,
    msg: '',
    data: { items, total: items.length, page: 1, page_size: PAGE_LIMIT },
  })
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  // 事件处理器表挂在 `Map` 上，`clearAllMocks()` 清不掉——不清的话上一次挂载注册的处理器
  // 会留在表里，「页面是否注册了某事件」这类断言会靠残留**假通过**。
  LAYUI.formHandlers.clear()
  LAYUI.tableHandlers.clear()
  LAYUI.msgs.length = 0
  LAYUI.state.bootstrapSawMarkup = true
  LAYUI.state.initSawMarkup = false
  LAYUI.layer.open.mockReturnValue(7)
  LAYUI.layer.confirm.mockReturnValue(3)
  vi.mocked(getRealNameSetting).mockResolvedValue({ ...SETTING })
  vi.mocked(updateRealNameSetting).mockResolvedValue({ ...SETTING })
  armQuotaGate()
})

afterEach(() => {
  document.body.innerHTML = ''
})

/* ────────────────────────── 声明式部分（复刻值） ────────────────────────── */

describe('A. 列声明：10 个标题列 + 1 个勾选列（逐条照抄 RnameCertLogAndSetting.js L40-100）', () => {
  it('11 列，文案序列与宽度逐条等于对标', () => {
    expect(COLS_FOR_TEST).toHaveLength(11)
    expect(COLS_FOR_TEST.map((c) => c.title)).toEqual([
      undefined,
      '系统用户名称',
      '真实姓名',
      '身份证号',
      '性别',
      '年龄',
      '认证方式',
      '认证结果',
      '申请时间',
      '认证时间',
      '认证图片',
    ])
    expect(COLS_FOR_TEST.map((c) => c.width)).toEqual([
      30, 150, 120, 130, 60, 60, 75, 100, 130, 130, 90,
    ])
    // ⛔ 钉**字面值**、不钉 `PAGE_LIMITS` 那种常量：拿常量比自己会让「常量被改坏」照样绿（E-0024）。
    expect(COLS_FOR_TEST.map((c) => c.align)).toEqual([
      undefined,
      'center',
      'center',
      'center',
      'center',
      'center',
      'center',
      'center',
      'center',
      'center',
      'center',
    ])
  })

  it('勾选列带 type:checkbox（缺它会静默丢列，探针不会报）', () => {
    expect(COLS_FOR_TEST[0]?.type).toBe('checkbox')
    expect(COLS_FOR_TEST[0]?.fixed).toBe('left')
  })

  it('「性别」是本页唯一 unresize:true 的数据列（L54）', () => {
    expect(COLS_FOR_TEST.filter((c) => c.unresize === true).map((c) => c.title)).toEqual(['性别'])
  })

  it('本页**没有操作列**（对标 cols 里既没有 toolbar 列，也没有 #baroperating 模板）', () => {
    expect(COLS_FOR_TEST.some((c) => c.toolbar !== undefined)).toBe(false)
  })

  it('所有列都有 width ⇒ 没有自动列', () => {
    expect(COLS_FOR_TEST.every((c) => typeof c.width === 'number')).toBe(true)
  })
})

describe('B. 表格渲染参数（复刻值）', () => {
  it('toolbar / 生效的 defaultToolbar / 分页（limit 20、**只有一档** [20]）', () => {
    expect(TABLE_OPTIONS.elem).toBe(TABLE_ELEM)
    expect(TABLE_OPTIONS.toolbar).toBe(TOOLBAR_ELEM)
    expect(TOOLBAR_ELEM).toBe('#toolbarDemo')
    expect(TABLE_OPTIONS.defaultToolbar).toEqual(['filter'])
    expect(DEFAULT_TOOLBAR).toEqual(['filter'])
    expect(TABLE_OPTIONS.page).toEqual({ limit: 20, limits: [20] })
    expect(PAGE_LIMIT).toBe(20)
    expect(PAGE_LIMITS).toEqual([20])
  })

  it('端点 = 我们的（对标的 `/UserAjax/GetUserRnameCertLogList` 不再出现），分页参数名是 `page`/`limit`', () => {
    expect(TABLE_OPTIONS.url).toBe('/api/accounts/realname-logs/')
    expect(TABLE_OPTIONS.method).toBe('get')
    // ⚠️ 与账号列表页**不同名**：后端 `RealNameLogPagination.page_size_query_param = "limit"`。
    expect(TABLE_OPTIONS.request).toEqual({ pageName: 'page', limitName: 'limit' })
  })
})

describe('C. 接口行 → 列数据', () => {
  it('10 个列字段逐条落到对的键上（含脱敏证件号与空 cert_image）', () => {
    expect(toRow(API_ROW)).toEqual({
      userName: 'kefu_jia',
      realName: '测试姓名甲',
      idCardNo: '110***********12',
      gender: '男',
      age: 17,
      certMethod: 'basic',
      certResult: 'pass',
      appliedAt: '2026-10-10 09:15:00',
      certifiedAt: '2026-10-10 09:15:03',
      certImage: '',
    })
  })

  it('`certified_at` 为 null ⇒ 空串（⛔ 不写「未认证」这类占位）', () => {
    expect(toRow({ ...API_ROW, certified_at: null }).certifiedAt).toBe('')
  })

  it('`age` 为 null ⇒ **保持 null**（⛔ 不 `?? 0`：那造出一个「0 岁」的假事实）', () => {
    expect(toRow({ ...API_ROW, age: null }).age).toBeNull()
  })

  it('「身份证号」列的值带 title 文案（逐字照抄 JS L49），且**没有**被二次转义', () => {
    const cell = renderCell(colByTitle(buildCols(), '身份证号'), toRow(API_ROW))
    const span = cell.querySelector('span')!
    expect(span.getAttribute('title')).toBe(ID_CARD_TITLE)
    expect(span.textContent).toBe('110***********12')
    // 脱敏值本身不含特殊字符 ⇒ 不该出现实体（双重转义的症状）。
    expect(span.innerHTML).toBe('110***********12')
  })

  it('「认证图片」列的值 = 「点击查看」的蓝链（`#31bdec`）与两个 data 属性（逐字照抄 JS L97）', () => {
    const cell = renderCell(colByTitle(buildCols(), '认证图片'), toRow(API_ROW))
    const a = cell.querySelector('a')!
    expect(a.getAttribute('style')).toBe('color:#31bdec')
    expect(a.getAttribute('title')).toBe('认证图片只保存1年')
    expect(a.className).toBe(CERT_IMAGE_CLASS)
    expect(a.textContent).toBe('点击查看')
    expect(a.dataset.img).toBe('')
    expect(a.dataset.uname).toBe('kefu_jia')
  })
})

describe('D. 两列徽章两态 + 年龄红字（值一律照抄证据件 JS）', () => {
  it('认证方式：`basic` ⇒ 绿「基础认证」；别的取值 ⇒ 橙「人脸加强」（默认那一支是橙）', () => {
    const col = colByTitle(buildCols(), '认证方式')
    const basic = renderCell(col, { ...toRow(API_ROW), certMethod: 'basic' }).innerHTML
    expect(basic).toBe('<span class="layui-badge layui-bg-green">基础认证</span>')
    const face = renderCell(col, { ...toRow(API_ROW), certMethod: 'face' }).innerHTML
    expect(face).toBe('<span class="layui-badge layui-bg-orange">人脸加强</span>')
  })

  it('认证结果：`pass` ⇒ 绿「通过认证」；`fail` ⇒ **灰**徽章「未通过认证」', () => {
    const col = colByTitle(buildCols(), '认证结果')
    const pass = renderCell(col, { ...toRow(API_ROW), certResult: 'pass' }).innerHTML
    expect(pass).toBe('<span class="layui-badge layui-bg-green">通过认证</span>')
    const fail = renderCell(col, { ...toRow(API_ROW), certResult: 'fail' }).innerHTML
    expect(fail).toBe('<span class="layui-badge">未通过认证</span>')
    // 灰徽章**不带** `layui-bg-*` —— 逐字照抄 JS L76 的 `<span class="layui-badge">`。
    expect(fail).not.toContain('layui-bg-')
  })

  it('年龄 17（<18）⇒ `#ff5722` + `lay-tips="年龄异常，请谨慎录用"`（两处值都逐字照抄 L60）', () => {
    const col = colByTitle(buildCols(), '年龄')
    const html = renderCell(col, { ...toRow(API_ROW), age: 17 }).innerHTML
    expect(html).toBe(
      `<span style="color:#ff5722" lay-tips="${ABNORMAL_AGE_TIPS}">17</span>`,
    )
    expect(ABNORMAL_AGE_TIPS).toBe('年龄异常，请谨慎录用')
  })

  it('年龄 >45 同样标红；18~45 **不**标红；`null` **不**标红（⛔ 不把「没算出来」说成年龄异常）', () => {
    const col = colByTitle(buildCols(), '年龄')
    const age = (value: number | null): string =>
      renderCell(col, { ...toRow(API_ROW), age: value }).innerHTML
    // 46 是 >45 的那一侧；45 与 18 是**边界内侧**（判据是 `<18 || >45`，两侧都是开区间）。
    expect(age(46)).toContain('#ff5722')
    expect(age(45)).toBe('<span class="">45</span>')
    expect(age(18)).toBe('<span class="">18</span>')
    // ⚠️ `null` 必须**不**标红：`Number(null) === 0 < 18` 会让每一行「没算出来」的都变红字。
    expect(age(null)).toBe('<span class=""></span>')
    expect(age(null)).not.toContain('#ff5722')
  })
})

describe('E. 日期默认值 = **近 30 天**（业务方 2026-10-10 拍板；⛔ 不照抄证据件的死值）', () => {
  it('`today-29d` ~ `today`（含今天共 30 天），本地时区、日期形状', () => {
    expect(defaultRealNameDateRange(new Date(2026, 9, 10, 12, 0, 0))).toEqual({
      sDate: '2026-09-11',
      eDate: '2026-10-10',
    })
  })

  it('跨月 / 跨年按自然日回退（不出现「9 月 0 日」这种串）', () => {
    expect(defaultRealNameDateRange(new Date(2026, 9, 1, 0, 0, 0))).toEqual({
      sDate: '2026-09-02',
      eDate: '2026-10-01',
    })
    expect(defaultRealNameDateRange(new Date(2027, 0, 5, 0, 0, 0))).toEqual({
      sDate: '2026-12-07',
      eDate: '2027-01-05',
    })
  })

  it('证据件里那对死值**不在**页面上（照抄它就是「快照日之后的记录默认看不见」）', () => {
    // 证据件确实写着 2026-08-20 / 2026-09-20（那是它服务端按取数当天渲染的）——
    // 这里断言的是**我们的代码不产出它们**。
    expect(EVIDENCE).toContain('value="2026-08-20"')
    const { sDate, eDate } = defaultRealNameDateRange(new Date(2026, 9, 10))
    expect([sDate, eDate]).not.toContain('2026-08-20')
    expect([sDate, eDate]).not.toContain('2026-09-20')
  })
})

describe('F. 控件名 → 接口参数名（唯一形状出口在 api 模块）', () => {
  it('`certStatus` 控件 → **`status`** 参数（⛔ 不照控件名写成 `cert_status`）', () => {
    expect(
      toLogParams({ sDate: '2026-09-11', eDate: '2026-10-10', certStatus: '2', key1: 'a' }),
    ).toEqual({
      sdate: '2026-09-11',
      edate: '2026-10-10',
      status: '2',
      key1: 'a',
      key2: '',
      key3: '',
    })
  })

  it('分页参数名是 `page` / `limit`（⛔ 不是账号列表那页的 `page_size`）', () => {
    expect(toLogParams({ sDate: '2026-10-01' }, 2)).toMatchObject({
      page: 2,
      limit: PAGE_LIMIT,
    })
    expect(Object.keys(buildRealNameLogParams(toLogParams({}, 2), true)).sort()).toEqual([
      'limit',
      'page',
    ])
  })

  it('`status="0"`（全部）是**真实取值**、必须原样发出去（⛔ 不被「空值归一化」丢掉）', () => {
    const params: RealNameLogParams = toLogParams({ certStatus: '0' })
    expect(buildRealNameLogParams(params, true).status).toBe('0')
    // 两段式的第一段里它**不在**（首屏只发日期）。
    expect(buildRealNameLogParams(params, false)).not.toHaveProperty('status')
  })

  it('空串键一律不发（`sdate`/`edate` 空 ⇒ 请求里没有它们）', () => {
    expect(buildRealNameLogParams(toLogParams({ certStatus: '1', key1: '  ' }), true)).toEqual({
      status: '1',
    })
  })
})

/* ────────────────────────── 摘挂载 / 真交互 ────────────────────────── */

describe('G. 摘挂载：先 bootstrapIsland 再塞标记，最后 initIslandElements（陷阱 ③）', () => {
  it('两个快照：bootstrap 时标记不在、init 时标记已在', async () => {
    await mountPage()
    expect(LAYUI.state.bootstrapSawMarkup).toBe(false)
    expect(LAYUI.state.initSawMarkup).toBe(true)
  })

  it('证据件的筛选栏 / 工具栏模板 / 配置弹层标记都在页面上，且日期框拿到近 30 天默认值', async () => {
    const root = await mountPage()
    expect(root.querySelector('#tableList')).toBeTruthy()
    expect(root.querySelector('#sDate')).toBeTruthy()
    expect(root.querySelector('#eDate')).toBeTruthy()
    expect(root.querySelector('#certStatus')).toBeTruthy()
    expect(root.querySelector('#key3')?.getAttribute('maxlength')).toBe('18')
    expect(root.querySelector('.settingDiv')).toBeTruthy()
    // 两个日期框交给 laydate，参数照对标 JS L19-26（`type: 'date'`，**没有** min/max）。
    expect(LAYUI.laydate.render).toHaveBeenCalledWith({ elem: '#sDate', type: 'date' })
    expect(LAYUI.laydate.render).toHaveBeenCalledWith({ elem: '#eDate', type: 'date' })
    const { sDate, eDate } = defaultRealNameDateRange()
    expect(root.querySelector<HTMLInputElement>('#sDate')!.value).toBe(sDate)
    expect(root.querySelector<HTMLInputElement>('#eDate')!.value).toBe(eDate)
    expect(sDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('分页参数名是 `limit`（与账号列表页 `page_size` 不同名）——由真渲染参数钉住', async () => {
    await mountPage()
    expect(renderOpts().request).toEqual({ pageName: 'page', limitName: 'limit' })
  })
})

describe('H. ⭐ 两段式取数（对标 JS L27 vs L147-154）', () => {
  it('① 首屏**只发 `sdate`/`edate`**：`where` 里没有 `status`/`key1`/`key2`/`key3`', async () => {
    await mountPage()
    const where = renderOpts().where!
    // ⚠️ 键集合**恰好**是这四个：两个日期 + 两个**分页**参数（`page`/`limit` 由取数层给；
    //    对标那边这两个也是 layui 自己加的、不在它那句 `seachParam` 里）。
    //    ⛔ 钉恰好集合、⛔ 不用 `toContain`：多出一个筛选键就是「首屏把四个筛选也发了」，
    //    而那正是这条用例要挡的东西。
    expect(Object.keys(where).sort()).toEqual(['edate', 'limit', 'page', 'sdate'])
    for (const key of ['status', 'key1', 'key2', 'key3']) {
      expect(where, `首屏不该带 ${key}`).not.toHaveProperty(key)
    }
    // 值就是筛选栏里显示的那两个日期（不是全表最新 20 条）。
    const root = document.querySelector<HTMLInputElement>('#sDate')!
    expect(where.sdate).toBe(root.value)
  })

  it('② 点「搜索」后请求**带上** `status` 与三个关键词（值现读 DOM）', async () => {
    const root = await mountPage()
    // 与真人同序：先改筛选栏，再点搜索。
    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-09-11'
    root.querySelector<HTMLInputElement>('#eDate')!.value = '2026-10-10'
    root.querySelector<HTMLSelectElement>('#certStatus')!.value = '1'
    root.querySelector<HTMLInputElement>('#key1')!.value = 'kefu_jia'
    root.querySelector<HTMLInputElement>('#key2')!.value = '测试姓名甲'
    root.querySelector<HTMLInputElement>('#key3')!.value = '110101'

    fireFormSubmit('seachBtn')

    const { where, page } = lastReload()
    expect(page).toEqual({ curr: 1 })
    expect(where).toEqual({
      sdate: '2026-09-11',
      edate: '2026-10-10',
      status: '1',
      key1: 'kefu_jia',
      key2: '测试姓名甲',
      key3: '110101',
      page: 1,
      limit: PAGE_LIMIT,
    })
  })

  it('「搜索」的取值是**现读 DOM**（改完再点才生效；不是挂载时快照）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLInputElement>('#key1')!.value = '第二次'
    fireFormSubmit('seachBtn')
    expect(lastReload().where.key1).toBe('第二次')
    root.querySelector<HTMLInputElement>('#key1')!.value = '第三次'
    fireFormSubmit('seachBtn')
    expect(lastReload().where.key1).toBe('第三次')
    expect(lastReload().page).toEqual({ curr: 1 })
  })

  it('工具栏「刷新列表」重取、回第 1 页；别的事件**什么都不做**（工具栏只有那三颗）', async () => {
    await mountPage()
    fireTableEvent('toolbar', { event: 'refresh' })
    expect(lastReload().page).toEqual({ curr: 1 })
    const before = LAYUI.table.reload.mock.calls.length
    fireTableEvent('toolbar', { event: 'something_else' })
    expect(LAYUI.table.reload.mock.calls.length).toBe(before)
  })

  /**
   * ⭐ R3-1 + R3-3：状态机的**两半**。
   *
   * **分辨什么**：把 `reload` 改回「没点过搜索就只发日期」（曾经的写法）⇒ 这条**必须红**
   * （`where` 里会只剩 `sdate`/`edate`/`page`/`limit`）。
   * 对标依据：`RnameCertLogAndSetting.js` 的 `reloadOrderList()`（**L147-154**）**每次都**从 DOM
   * 现读 `sdate`/`edate`/`status`/`key1`/`key2`/`key3`，而「刷新」（**:115-117**）与「搜索」
   * （**:146**）调的是**同一个函数** ⇒ 刷新**也带筛选**。
   * ⚠️ 与「首屏只发两个日期」是**同一件事的两半**（那一半见本块第 ① 条）。
   */
  it('⭐ R3-1/R3-3：筛选改完**直接点「刷新列表」**⇒ `where` 也带全部 6 项（对标 `reloadOrderList`）', async () => {
    const root = await mountPage()
    // 一次「搜索」都没点过 —— 这正是曾经的写法会漏掉筛选的那个场景。
    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-09-11'
    root.querySelector<HTMLInputElement>('#eDate')!.value = '2026-10-10'
    root.querySelector<HTMLSelectElement>('#certStatus')!.value = '1'
    root.querySelector<HTMLInputElement>('#key1')!.value = 'kefu_jia'
    root.querySelector<HTMLInputElement>('#key2')!.value = '测试姓名甲'
    root.querySelector<HTMLInputElement>('#key3')!.value = '110101'

    fireTableEvent('toolbar', { event: 'refresh' })

    const { where, page } = lastReload()
    expect(page).toEqual({ curr: 1 })
    // 键集合**恰好**是这 8 个（⛔ 不用 `toContain`：少一个筛选键这条就失去判别力）。
    expect(Object.keys(where).sort()).toEqual([
      'edate',
      'key1',
      'key2',
      'key3',
      'limit',
      'page',
      'sdate',
      'status',
    ])
    // 值 = **点刷新那一刻** DOM 里的值（不是挂载时的快照，也不是证据件的默认值）。
    expect(where).toEqual({
      sdate: '2026-09-11',
      edate: '2026-10-10',
      status: '1',
      key1: 'kefu_jia',
      key2: '测试姓名甲',
      key3: '110101',
      page: 1,
      limit: PAGE_LIMIT,
    })
  })

  it('⭐ R3-3：**搜索之后再点「刷新列表」**⇒ 筛选**一个都不丢**（两半的另一半）', async () => {
    const root = await mountPage()
    // 六项全填（空串的键本来就不发 ⇒ 要考「丢没丢」就得让六项都有值）。
    root.querySelector<HTMLInputElement>('#sDate')!.value = '2026-09-11'
    root.querySelector<HTMLInputElement>('#eDate')!.value = '2026-10-10'
    root.querySelector<HTMLSelectElement>('#certStatus')!.value = '2'
    root.querySelector<HTMLInputElement>('#key1')!.value = '第一次'
    root.querySelector<HTMLInputElement>('#key2')!.value = '测试姓名甲'
    root.querySelector<HTMLInputElement>('#key3')!.value = '110101'
    fireFormSubmit('seachBtn')
    // 搜索那一发：全参（与上一条同一形状，这里只确认它真的带上了）。
    expect(Object.keys(lastReload().where).sort()).toEqual([
      'edate',
      'key1',
      'key2',
      'key3',
      'limit',
      'page',
      'sdate',
      'status',
    ])
    // 搜索之后**再改**筛选栏，然后点刷新 ⇒ 带的是**此刻**的 DOM 值。
    root.querySelector<HTMLInputElement>('#key1')!.value = '改过之后再刷新'
    // 多插一发，确保下面读到的确实是「刷新」那一发（⛔ 不是搜索那一发）。
    const beforeRefresh = LAYUI.table.reload.mock.calls.length

    fireTableEvent('toolbar', { event: 'refresh' })

    expect(LAYUI.table.reload.mock.calls.length).toBe(beforeRefresh + 1)
    const { where } = lastReload()
    expect(where.status).toBe('2')
    expect(where.key1).toBe('改过之后再刷新')
    // 刷新**不把筛选丢掉**：6 项一个不少（这是本条的判据）。
    expect(Object.keys(where).sort()).toEqual([
      'edate',
      'key1',
      'key2',
      'key3',
      'limit',
      'page',
      'sdate',
      'status',
    ])
  })
})

describe('I. ⭐ 配置弹层（文案逐字来自证据件；值来自服务端）', () => {
  it('③ 点「配置实名认证」⇒ 开层：标题 / area / skin / anim / 那 4 项的**问句与选项**都逐字正确', async () => {
    await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()

    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const args = (LAYUI.layer.open.mock.calls as unknown as [Record<string, unknown>][])[0]![0]
    expect(args.title).toBe('实名认证相关设置')
    expect(args.area).toEqual(['620px', '480px'])
    expect(args.skin).toBe('layui-layer-lan')
    expect(args.anim).toBe(3)
    expect(args.type).toBe(1)
    expect(args.shade).toEqual([0.3, '#333'])
    expect(args.shadeClose).toBe(true)

    // 4 项的问句与选项**在证据件里**（本页直接渲染它，一个字没重写）⇒ 判据打在证据件原文上。
    const questions = [
      '1、是否启用系统子账号人脸识别实名认证功能？（默认不启用，只有启用了子账号那边才能发起实名认证）',
      '2、是否不允许没有通过实名认证的子账号在内部抢单大厅接单？（默认允许）',
      '3、通过系统给子账号在线结算打款时是否要求该用户必须通过了实名认证？（默认不要求此条件）',
      '4、只允许指定范围区间的年龄用户通过实名，不符合年龄范围的将会被拒绝通过(为空或填0代表不限制)',
    ]
    for (const q of questions) expect(EVIDENCE).toContain(q)
    // 四个字段名（证据件的 `name`）——回填与提交都按它们走。
    for (const name of ['CertSwicth', 'CertPassRecOrder', 'CertOnineSett', 'CertMinAge', 'CertMaxAge']) {
      expect(EVIDENCE).toContain(`name="${name}"`)
    }
    // 三组 radio 的选项文案（`title="…"` 是证据件原文）。
    for (const title of ['不启用', '启用', '允许', '不允许', '不要求', '要求实名']) {
      expect(EVIDENCE).toContain(`title="${title}"`)
    }
    // 「保存设置」那颗按钮。
    expect(EVIDENCE).toContain('lay-filter="saveSettingBtn"')
  })

  it('③（续）4 项控件的**值** = 服务端回的那一份（⛔ 不钉代码里的常量）', async () => {
    const root = await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()

    const panel = root.querySelector<HTMLElement>('.settingDiv')!
    const checked = (name: string): string | undefined =>
      panel.querySelector<HTMLInputElement>(`input[name="${name}"]:checked`)?.value
    // 服务端回的是「启用 / 不允许 / 不要求」+ 18~45 —— 与证据件里的默认值（三个 N、两个 0）
    // **每一格都不同** ⇒ 这条断言真的有判别力（回填没做的话每一格都会红）。
    expect(checked('CertSwicth')).toBe('Y')
    expect(checked('CertPassRecOrder')).toBe('Y')
    expect(checked('CertOnineSett')).toBe('N')
    expect(panel.querySelector<HTMLInputElement>('input[name="CertMinAge"]')!.value).toBe('18')
    expect(panel.querySelector<HTMLInputElement>('input[name="CertMaxAge"]')!.value).toBe('45')
    // `form.render('radio')` 必须重跑：三颗 radio 的**壳**要跟着属性重画。
    expect(LAYUI.form.render).toHaveBeenCalledWith('radio')
  })

  it('⭐ `min_age`/`max_age` 的 `0` 是**合法值**：回填成 "0" 而不是清成空串（E-0021 / E-0074）', async () => {
    vi.mocked(getRealNameSetting).mockResolvedValue({
      ...SETTING,
      min_age: 0,
      max_age: 0,
    })
    const root = await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()
    const panel = root.querySelector<HTMLElement>('.settingDiv')!
    expect(panel.querySelector<HTMLInputElement>('input[name="CertMinAge"]')!.value).toBe('0')
    expect(panel.querySelector<HTMLInputElement>('input[name="CertMaxAge"]')!.value).toBe('0')
    // 回填后**再读一遍**仍然是 0（读回来的形状 == 写下去的形状）。
    expect(readSettingForm(panel)).toMatchObject({ min_age: 0, max_age: 0 })
  })

  it('⭐ 弹层底部那行**两态提示**在且逐字（2026-10-16 改准；接单侧已生效、结算侧仍只存，⛔ 文案说错也是静默说谎）', async () => {
    const root = await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()
    const panel = root.querySelector<HTMLElement>('.settingDiv')!
    const note = panel.querySelector<HTMLElement>('[data-realname-note]')
    expect(note, '配置弹层底部必须有一行两态提示（spec §14-P11 增设、2026-10-16 §2.4 改准）').not.toBeNull()
    expect(note!.textContent).toBe('「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用')
    // 对标**没有**这一行 ⇒ 它必须是运行期追加的（证据件本体一个字节没动）。
    expect(EVIDENCE).not.toContain('「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用')
  })

  it('④ 改一项 + 点「保存设置」⇒ 发出 PATCH，body 里含被改的那个字段（五个键都带）', async () => {
    const root = await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()

    const panel = root.querySelector<HTMLElement>('.settingDiv')!
    // 用户把「启用」从 Y 改成 N。
    const radios = Array.from(panel.querySelectorAll<HTMLInputElement>('input[name="CertSwicth"]'))
    const off = radios.find((r) => r.value === 'N')!
    for (const r of radios) r.checked = r === off
    fireFormSubmit('saveSettingBtn')
    await flush()

    expect(updateRealNameSetting).toHaveBeenCalledTimes(1)
    expect(vi.mocked(updateRealNameSetting).mock.calls[0]![0]).toEqual({
      enabled: false,
      block_claim_without_cert: true,
      require_cert_for_settlement: false,
      min_age: 18,
      max_age: 45,
    })
    // 保存成功后关层 + 一句反馈（⛔ 不能「点了没反应」）。
    expect(LAYUI.layer.close).toHaveBeenCalledWith(7)
    expect(LAYUI.msgs).toContain('保存成功')
  })

  it('保存失败 ⇒ **可读提示**、层不关（⛔ 不静默）', async () => {
    vi.mocked(updateRealNameSetting).mockRejectedValue(new Error('缺少权限：system_user.realname_records'))
    await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()
    fireFormSubmit('saveSettingBtn')
    await flush()
    expect(LAYUI.msgs).toContain('缺少权限：system_user.realname_records')
    expect(LAYUI.layer.close).not.toHaveBeenCalled()
    expect(LAYUI.msgs).not.toContain('保存成功')
  })

  it('取配置失败 ⇒ **不开层**、只给一句可读提示（开一个「全是默认值」的层比不开更糟）', async () => {
    vi.mocked(getRealNameSetting).mockRejectedValue(new Error('网络异常，请检查连接'))
    await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()
    expect(LAYUI.layer.open).not.toHaveBeenCalled()
    expect(LAYUI.msgs).toContain('网络异常，请检查连接')
  })

  /**
   * ⭐ R3-2：**模拟真实 layui 把面板搬出 `root`**。
   *
   * **分辨什么**：页里 `findSettingsPanel()` 那两句是「先从 `document` 的
   * `.layui-layer-content > .settingDiv` 找、找不到再从 `root` 找」。真 layui 的
   * `layer.open({type:1})` 会把 `content` 那个节点 **搬进 `document.body` 下的
   * `.layui-layer-content`**（vendored `public/layui/layui.js` 里那一支：`r.parents('.layui-layer')[0]
   * || r.show().addClass('layui-layer-wrap').wrap(e[1])`，随后 `c.append(e[0])`，`c = $('body')`）
   * ⇒ 弹层一开，`root` 里就**再也没有**它了；真浏览器里**只有文档级那一句**能用。
   * 本文件的替身**不搬** ⇒ 那条分支平时**不可达**（去掉它也不会红）。
   * 所以这条用例自己搬：**把面板从 `root` 挪进 `body` 的 `.layui-layer-content`**，
   * 再点「保存设置」。
   *
   * **若兜底（文档级）那一句失效** ⇒ `findSettingsPanel()` 回 `null` ⇒
   * `readSettingForm(null)` 回**证据件的默认值**（三颗 `N` + 两个 `0`）⇒ 这条**必须红**：
   * 断言写的是**服务端那份真值**（`enabled:true` / `block:true` / `18` / `45`），与默认值**每一格都不同**。
   */
  it('⭐ R3-2：面板被 `layer.open` 搬出 `root` 之后，「保存设置」发的仍是**弹层里的真值**（⛔ 不是默认值）', async () => {
    const root = await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()

    // —— 这一段就是「真 layui 干了什么」：把面板从 `root` 搬进 `body` 的 `.layui-layer-content`。
    const panel = root.querySelector<HTMLElement>('.settingDiv')!
    const layerBox = document.createElement('div')
    layerBox.className = 'layui-layer layui-layer-page'
    const layerContent = document.createElement('div')
    layerContent.className = 'layui-layer-content'
    layerBox.appendChild(layerContent)
    document.body.appendChild(layerBox)
    layerContent.appendChild(panel) // ← 搬走（真 layui 用的是 `.wrap(...)` + `.append(...)`）
    expect(root.querySelector('.settingDiv'), '面板必须已经**不在** root 里（否则这条用例没在考兜底分支）').toBeNull()
    expect(document.querySelector('.layui-layer-content > .settingDiv')).toBe(panel)

    // 用户改一项：把「启用」从服务端的 Y 改成 N（改**属性**与属性值，与 `fillSettingForm` 同形）。
    const radios = Array.from(panel.querySelectorAll<HTMLInputElement>('input[name="CertSwicth"]'))
    const off = radios.find((r) => r.value === 'N')!
    for (const r of radios) {
      const on = r === off
      r.checked = on
      if (on) r.setAttribute('checked', 'checked')
      else r.removeAttribute('checked')
    }

    fireFormSubmit('saveSettingBtn')
    await flush()

    expect(updateRealNameSetting).toHaveBeenCalledTimes(1)
    // ⚠️ 这五个值**逐格**都是弹层里的真值：默认值那一份是 `false/false/false/0/0`，
    //    若兜底分支失效，下面这几行会以「默认值」红掉。
    expect(vi.mocked(updateRealNameSetting).mock.calls[0]![0]).toEqual({
      enabled: false,
      block_claim_without_cert: true,
      require_cert_for_settlement: false,
      min_age: 18,
      max_age: 45,
    })
    // ⛔ 兜底分支失效时页面**不会**报错、只会静默改配置 ⇒ 这两条是「不许静默」的旁证。
    expect(LAYUI.msgs).not.toContain('实名认证设置面板未渲染，请刷新页面重试')
    expect(LAYUI.msgs).toContain('保存成功')
  })

  it('⭐ R3-4：连开两次配置层 ⇒ 那行中立提示**只有一行**（幂等 guard 的判据）', async () => {
    const root = await mountPage()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()
    // 关层（真 layui 会把面板原样放回并 `display:none`；替身不搬 ⇒ 这里只关层就行），
    // 再开第二次 —— 模拟用户反复点「配置实名认证」。
    fireFormSubmit('saveSettingBtn')
    await flush()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()

    expect(LAYUI.layer.open).toHaveBeenCalledTimes(2)
    const notes = root.querySelectorAll('[data-realname-note]')
    expect(notes, '中立提示只能有一行（连开两次层不许插第二遍）').toHaveLength(1)
    expect(notes[0]!.textContent).toBe('「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用')
  })
})

describe('J. ⭐ 配置按钮：点击开「配置实名资源包」弹窗（T3，业务方 2026-10-16 拍板改应用内配置）', () => {
  it('⑤ 点它 ⇒ 打开配置弹窗（`layer.open`，先取包列表再开层），⛔ 不许点了没反应', async () => {
    vi.mocked(listRealNamePackages).mockResolvedValue([])
    await mountPage()
    LAYUI.msgs.length = 0
    LAYUI.layer.open.mockClear()
    fireTableEvent('toolbar', { event: 'buyBtn' })
    await flush()
    // ⛔ 旧的那条 toast 路径已删：点按钮不再 `layer.msg`，而是开层。
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
    const opts = (LAYUI.layer.open.mock.calls as unknown as [Record<string, unknown>][])[0]![0]
    expect(opts.title).toBe('配置实名资源包')
    expect(LAYUI.msgs).toEqual([])
  })

  it('本页源码里**没有**对标那条站外购买链接，也没有 `#jdpbuykey` 的读法（那是一段我们拿不到的租户值）', () => {
    // 对标 `RnameCertLogAndSetting.js:129-146` 那一支读 `#jdpbuykey`、再
    // `util.openWin('https://www.jidanpu.com/buy/BuyRnameCertPackge?u=' + key)`。
    // 我们**没有**那个绑定 ID（证据件里那个隐藏域是空的、还留了「PII 已清空」的行内注释）⇒
    // 照抄那一支只会永远走「还没有绑定」那个 alert、而且外链是**对标物的商城**。
    const source = codeOnly(
      fs.readFileSync(
        path.resolve(
          path.dirname(fileURLToPath(import.meta.url)),
          '../user-rnamecertlogandsetting.ts',
        ),
        'utf8',
      ),
    )
    expect(source).not.toContain('jidanpu')
    expect(source).not.toContain('BuyRnameCertPackge')
    // ⚠️ `#jdpbuykey` 那个隐藏域**留在证据件里**（它是标记层的一部分，本页不动证据件），
    // 但页面源码不许去读它。
    expect(EVIDENCE).toContain('id="jdpbuykey"')
    expect(source).not.toContain('jdpbuykey')
  })
})

describe('K. ⭐ 余量 N 出现在按钮文案里（**由服务端数据渲染**，不是证据件那个字面量 0）', () => {
  it('⑥ 服务端回 37 ⇒ 按钮文案是「…(当前余量37次)」', async () => {
    const root = await mountPage()
    await flush()
    const btn = root.querySelector<HTMLElement>('[lay-event="buyBtn"]')!
    expect(btn.textContent).toBe(`配置实名资源包(当前余量${REMAINING}次)`)
    expect(REMAINING).not.toBe(0)
    // 证据件里那个字面量（「购买实名认证套餐包(当前余量0次)」）还在证据件里——
    // 文案是运行期替换的，⛔ 本页不改证据件；按钮上那一格不是它（T3 换成「配置实名资源包」）。
    expect(EVIDENCE).toContain('购买实名认证套餐包(当前余量0次)')
    expect(btn.textContent).not.toBe('购买实名认证套餐包(当前余量0次)')
  })

  it('余量 0 也照实写 0（`0` 是合法读数，⛔ 不当成「没拿到」）', async () => {
    const root = await mountPage({ remaining: 0 })
    await flush()
    expect(root.querySelector<HTMLElement>('[lay-event="buyBtn"]')!.textContent).toBe(
      '配置实名资源包(当前余量0次)',
    )
    expect(buyButtonLabel(0)).toBe('配置实名资源包(当前余量0次)')
    // ⭐ `0` 与 `null` 是**两种不同的状态**：按钮文案两档相同（都回落到证据件的字面量）
    // ⇒ 靠提示分家。`0` 这一档说的是「用完了」，⛔ 不是 `null` 那句「读不到」。
    expect(LAYUI.msgs).toContain(QUOTA_EXHAUSTED_TEXT)
    expect(LAYUI.msgs).not.toContain('暂时读不到本租户的实名认证剩余次数，请稍后重试')
  })

  it('余量读不到（后端回 `null`，= 跨 schema 读失败）⇒ 一句人话提示 + 按钮**不假装是 0 次这个读数**', async () => {
    // ⚠️ `null` 这一档必须在**挂载期**那一发上造（其余用例走 `mountPage()` 的默认读数）。
    const root = await mountPage({ remaining: null })
    await flush()
    expect(LAYUI.msgs).toContain('暂时读不到本租户的实名认证剩余次数，请稍后重试')
    // ⛔ 这一档**不许**说「余量已用完」——那是把「读不到」说成「读数 0」（`E-0021` 同族）。
    expect(LAYUI.msgs).not.toContain(QUOTA_EXHAUSTED_TEXT)
    // 按钮回落到证据件的字面量（唯一一个不承载租户信息的取值），但**必须**有那句提示，
    // ⛔ 不许静默留 0 装作「你就是没次数了」。
    expect(root.querySelector<HTMLElement>('[lay-event="buyBtn"]')!.textContent).toBe(
      '配置实名资源包(当前余量0次)',
    )
  })

  it('余量取数失败**不让整页失败**：只提示，列表与配置照常', async () => {
    await mountPage({ reject: '网络异常，请检查连接' })
    await flush()
    expect(LAYUI.msgs).toContain('网络异常，请检查连接')
    // 列表照样渲染了、配置弹层照样打得开。
    expect(renderOpts().where).toBeTruthy()
    fireTableEvent('toolbar', { event: 'settingBtn' })
    await flush()
    expect(LAYUI.layer.open).toHaveBeenCalledTimes(1)
  })
})

describe('M. ⭐ 配置实名资源包弹窗（T3，判据 8 的前端半）', () => {
  /** 合成掩码行（key_id 已是后端掩码；⛔ 夹具里没有、也不该有 secret）。 */
  const PKG_ROW = {
    id: 1,
    key_id: 'AKID***',
    total_quota: 250,
    used_quota: 30,
    expires_at: '2027-10-10T00:00:00+08:00',
    is_active: true,
    remark: '2026-10 购入备用包（合成备注）',
    created_at: '2026-10-16T10:00:00+08:00',
  }

  /** 点配置按钮并等弹窗渲染完。 */
  async function openPackagesDialog(): Promise<HTMLElement> {
    await mountPage()
    vi.mocked(listRealNamePackages).mockResolvedValue([PKG_ROW])
    fireTableEvent('toolbar', { event: 'buyBtn' })
    await flush()
    const panel = document.querySelector<HTMLElement>('.realnamePkgDiv')!
    expect(panel, '弹窗容器应当已经在页面上').not.toBeNull()
    return panel
  }

  it('列表渲染掩码行：key_id 原样展示（后端已掩码，前端不二次加工）、余量 / 状态 / 有效期逐格', async () => {
    const panel = await openPackagesDialog()
    const rows = Array.from(panel.querySelectorAll(`tr.${PKG_ROW_CLASS}`))
    expect(rows).toHaveLength(1)
    expect(rows[0]!.textContent).toContain('AKID***')
    expect(rows[0]!.textContent).toContain('220/250')
    expect(rows[0]!.textContent).toContain('启用')
    expect(rows[0]!.textContent).toContain('2027-10-10 00:00')
    expect(rows[0]!.textContent).toContain('2026-10 购入备用包（合成备注）')
    // ⛔ 弹窗里任何地方都不出现 secret 的形状（本夹具里根本没有那个串）。
    expect(panel.textContent).not.toContain('SYNTH')
  })

  it('备注含尖括号的包 ⇒ 渲染为文本不执行（存储型 XSS 钉子：pkgRowHtml 过 escapeHtml）', async () => {
    // 备注是表单里自己敲的用户可控串，而这一段走 `innerHTML` ⇒ 尖括号必须被转义成实体。
    await mountPage()
    vi.mocked(listRealNamePackages).mockResolvedValue([
      { ...PKG_ROW, id: 9, remark: '<img src=x onerror="alert(1)">' },
    ])
    fireTableEvent('toolbar', { event: 'buyBtn' })
    await flush()
    const panel = document.querySelector<HTMLElement>('.realnamePkgDiv')!
    const rows = Array.from(panel.querySelectorAll(`tr.${PKG_ROW_CLASS}`))
    expect(rows).toHaveLength(1)
    // 转义成实体后浏览器把它当**文本**渲染 ⇒ textContent 读回仍是原文。
    expect(rows[0]!.textContent).toContain('<img src=x onerror="alert(1)">')
    // ⛔ DOM 里不许真的长出 img / script 元素（innerHTML 注入一旦成立，这条就红）。
    expect(panel.querySelectorAll('img, script')).toHaveLength(0)
    // 实体形式确实在标记里——钉的是 escapeHtml 本身，不是「恰好没有 img」。
    expect(rows[0]!.innerHTML).toContain('&lt;img')
  })

  it('添加成功 ⇒ 载荷逐键、secret 框清空、列表重取（刷新）、成功提示', async () => {
    vi.mocked(createRealNamePackage).mockResolvedValue({ ...PKG_ROW, id: 2 })
    const panel = await openPackagesDialog()
    const set = (name: string, v: string) => {
      ;(panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)!).value = v
    }
    set(PKG_INPUT_NAMES.keyId, 'AKIDSYNTH0002')
    set(PKG_INPUT_NAMES.keySecret, 'SYNTH-SECRET-0002')
    set(PKG_INPUT_NAMES.totalQuota, '500')
    set(PKG_INPUT_NAMES.expiresAt, '2028-01-01')
    set(PKG_INPUT_NAMES.remark, '第二个包')
    const callsBefore = vi.mocked(listRealNamePackages).mock.calls.length
    LAYUI.msgs.length = 0
    ;(panel.querySelector<HTMLElement>('[data-pkg-submit]')!).click()
    await flush()

    expect(createRealNamePackage).toHaveBeenCalledWith({
      key_id: 'AKIDSYNTH0002',
      key_secret: 'SYNTH-SECRET-0002',
      total_quota: 500,
      expires_at: '2028-01-01',
      remark: '第二个包',
    })
    // secret 框提交后清空（⛔ 最贵的字符串不许留在 DOM 上）。
    const secretInput = panel.querySelector<HTMLInputElement>(
      `input[name="${PKG_INPUT_NAMES.keySecret}"]`,
    )!
    expect(secretInput.value).toBe('')
    expect(secretInput.getAttribute('type')).toBe('password')
    // 列表重取（刷新）+ 成功提示。
    expect(vi.mocked(listRealNamePackages).mock.calls.length).toBe(callsBefore + 1)
    expect(LAYUI.msgs).toContain(PKG_CREATED_TEXT)
  })

  it('添加失败 ⇒ 信封 msg 原样、secret 框仍清空、其余字段保留（用户只改要改的）', async () => {
    vi.mocked(createRealNamePackage).mockRejectedValue(
      new Error('密钥 Id AKID*** 已经录过了（本命令 ⛔ 不静默覆盖）：要换密钥请另建一个包（额度各自独立），旧的置 is_active=False 停用。'),
    )
    const panel = await openPackagesDialog()
    const set = (name: string, v: string) => {
      ;(panel.querySelector<HTMLInputElement>(`input[name="${name}"]`)!).value = v
    }
    set(PKG_INPUT_NAMES.keyId, 'AKID***')
    set(PKG_INPUT_NAMES.keySecret, 'SYNTH-SECRET-DUP')
    set(PKG_INPUT_NAMES.totalQuota, '10')
    LAYUI.msgs.length = 0
    ;(panel.querySelector<HTMLElement>('[data-pkg-submit]')!).click()
    await flush()

    expect(LAYUI.msgs[0]).toContain('已经录过了')
    expect(
      panel.querySelector<HTMLInputElement>(`input[name="${PKG_INPUT_NAMES.keySecret}"]`)!.value,
    ).toBe('')
    // 其它字段保留（失败重试不必重填）。
    expect(
      panel.querySelector<HTMLInputElement>(`input[name="${PKG_INPUT_NAMES.keyId}"]`)!.value,
    ).toBe('AKID***')
    expect(
      panel.querySelector<HTMLInputElement>(`input[name="${PKG_INPUT_NAMES.totalQuota}"]`)!.value,
    ).toBe('10')
  })

  it('列表读取失败 ⇒ 空态行给真实原因（⛔ 不是静默空白）', async () => {
    await mountPage()
    vi.mocked(listRealNamePackages).mockRejectedValue(new Error('读不到平台表'))
    fireTableEvent('toolbar', { event: 'buyBtn' })
    await flush()
    const panel = document.querySelector<HTMLElement>('.realnamePkgDiv')!
    expect(panel.querySelector('[data-pkg-rows]')!.textContent).toContain('读不到平台表')
  })
})

describe('L. ⭐ 认证图片：先弹法律提示原文 ⇒ 才 photos / 空值提示', () => {
  /** 驱动「点击查看」的**真产物**点击（连「templet 写哪个 data 属性」与「处理器读哪个属性」一起钉住）。 */
  function clickViewCertImage(root: HTMLElement, row: Row): HTMLElement {
    const cell = renderCell(colByTitle(buildCols(), '认证图片'), row)
    const anchor = cell.querySelector<HTMLElement>('a')!
    root.appendChild(anchor)
    anchor.click()
    return anchor
  }

  it('先弹 `layer.confirm`（文案**逐字**照抄 JS L168，按钮逐字照抄 L169），此时**不**开 photos', async () => {
    const root = await mountPage()
    clickViewCertImage(root, toRow(API_ROW))
    expect(LAYUI.layer.confirm).toHaveBeenCalledTimes(1)
    const [text, opts] = confirmCall()
    expect(text).toBe(CERT_IMAGE_NOTICE)
    expect(text).toContain('切不可随意将图片转发、传播、滥用')
    expect(text).toContain('否则以此引发的所有后果由您负责！')
    expect(opts.btn).toEqual(['我已知晓，保证不滥用', '关闭'])
    expect(CERT_IMAGE_NOTICE_BUTTONS).toEqual(['我已知晓，保证不滥用', '关闭'])
    // 还没点「我已知晓」⇒ 一张图都不许开。
    expect(LAYUI.layer.photos).not.toHaveBeenCalled()
  })

  it('`cert_image` 恒空 ⇒ 点「我已知晓」后提示「暂无认证图片」、**不置灰**、也不开 photos', async () => {
    const root = await mountPage()
    const anchor = clickViewCertImage(root, toRow(API_ROW))
    // **不置灰**：锚照对标渲染成蓝链（`artifactPage` 的「尚未开放」收口已经不在了，
    // 而 `AGENTS.md` §5-D 要求「无后端的部分照对标渲染、点了提示，**不置灰**」）。
    expect(anchor.getAttribute('style')).toBe('color:#31bdec')
    expect(anchor.hasAttribute('disabled')).toBe(false)
    expect(anchor.style.pointerEvents).toBe('')

    LAYUI.msgs.length = 0
    confirmCall()[2]()
    expect(LAYUI.msgs).toEqual([CERT_IMAGE_EMPTY_TEXT])
    expect(CERT_IMAGE_EMPTY_TEXT).toBe('暂无认证图片')
    expect(LAYUI.layer.photos).not.toHaveBeenCalled()
    // 提示框要关掉（对标在 `yes` 里显式 `layer.close`）。
    expect(LAYUI.layer.close).toHaveBeenCalledWith(3)
  })

  it('有图时（本批不可达，但那一支要照对标）⇒ 先关提示框，再 `layer.photos` 用 `data-img` / `data-uname`', async () => {
    const root = await mountPage()
    clickViewCertImage(root, { ...toRow(API_ROW), certImage: 'https://example.test/cert/1.png' })
    confirmCall()[2]()
    expect(LAYUI.layer.photos).toHaveBeenCalledTimes(1)
    const opts = LAYUI.layer.photos.mock.calls[0]![0] as {
      photos: { title: string; data: { alt: string; src: string }[] }
      footer: boolean
    }
    expect(opts.footer).toBe(false)
    expect(opts.photos.title).toBe('kefu_jia的认证图')
    expect(opts.photos.data[0]).toEqual({
      alt: 'kefu_jia的认证图片',
      pid: 0,
      src: 'https://example.test/cert/1.png',
    })
    expect(LAYUI.msgs).toEqual([])
  })

  it('点「关闭」那一支 ⇒ 什么都不做（对标给的是空回调，JS L186-187）', async () => {
    const root = await mountPage()
    clickViewCertImage(root, { ...toRow(API_ROW), certImage: 'https://example.test/cert/1.png' })
    const no = confirmCall()[3]
    expect(no).toBeTypeOf('function')
    no()
    expect(LAYUI.layer.photos).not.toHaveBeenCalled()
    expect(LAYUI.msgs).toEqual([])
  })
})

describe('M. 列表响应落到表格（`parseData` 翻信封）', () => {
  it('`{code,msg,count,data}`：count 取服务端 total、data 是映射后的行', async () => {
    await mountPage()
    const opts = renderOpts()
    const parsed = opts.parseData!({
      code: 0,
      msg: '',
      data: { items: [API_ROW], total: 41, page: 1, page_size: PAGE_LIMIT },
    })
    expect(parsed.code).toBe(0)
    expect(parsed.count).toBe(41)
    expect(parsed.data).toEqual([toRow(API_ROW)])
    // 10 个列字段，一个不多一个不少。
    expect(Object.keys(parsed.data[0]!).sort()).toEqual([
      'age',
      'appliedAt',
      'certImage',
      'certMethod',
      'certResult',
      'certifiedAt',
      'gender',
      'idCardNo',
      'realName',
      'userName',
    ])
  })

  it('空列表 ⇒ count 0 / data 空数组（⛔ 不是 undefined：`?? 0` / `?? []` 那一层要有判据）', async () => {
    await mountPage()
    const parsed = renderOpts().parseData!({ code: 0, msg: '', data: {} })
    expect(parsed.count).toBe(0)
    expect(parsed.data).toEqual([])
  })

  it('⭐ 整链：响应 → `parseData` → 逐列 `templet` 渲染（这条才证明「服务端的行真的画得出来」）', async () => {
    await mountPage()
    const before = renderOpts()
    expect(before.parseData).toBeTypeOf('function')
    driveResponse([API_ROW])
    // 逐列把真产物渲染出来（替身不渲染单元格 ⇒ 这一步是「响应到画面」之间唯一的判据）。
    const cells = buildCols().map((col) => {
      if (typeof col.templet === 'function') {
        return (col.templet as (d: Row) => string)(toRow(API_ROW))
      }
      return String((toRow(API_ROW) as unknown as Record<string, unknown>)[col.field as string] ?? '')
    })
    expect(cells).toHaveLength(11)
    expect(cells[1]).toBe('kefu_jia')
    expect(cells[2]).toBe('测试姓名甲')
    expect(cells[5]).toContain('#ff5722')
    expect(cells[6]).toContain('基础认证')
    expect(cells[7]).toContain('通过认证')
    expect(cells[10]).toContain('点击查看')
  })
})
