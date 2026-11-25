// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RequestMetricsPayload } from '@/api/requestMetrics'

/**
 * 【开放平台 → 性能分析】island 页的契约测试（2026-10-04，性能分析线 Task 3）。
 *
 * 这一页是**自有功能**（对标物里没有），所以没有证据件可对拍；本文件钉的是**有真实后果**的约定：
 *
 * 1. **驱动真交互**（`AGENTS.md` §5-G：自建 island 页必须有能红的交互用例）：
 *    改两个日期框 + 点「查询」⇒ **真发一条 GET**，且**查询串逐字正确**（键名是 spec §4 冻结的
 *    `date_from` / `date_to`），响应接着**真灌进三张表**（`table.reloadData` 的 `data` 就是返回值）；
 * 2. **三条 island 陷阱**（同 §5-G）：类名 / 选择器两个常量、先挂样式→再塞标记→最后
 *    `initIslandElements`（以**运行期 DOM** 判断，不是读源码）；
 * 3. **只读**（spec §8 第 3 条）：⛔ 不提供导出、⛔ 不发任何非 GET 请求；
 * 4. 默认时间窗 = **最近 7 天**（spec §1 第 3 条）。
 *
 * ## 手法（照 `thirdparty-jidanpu-sync.spec.ts`）
 * ⚠️ 那个先例文件已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）——本文件这一套照旧生效。
 *
 * - `vi.mock('@/layui/island')`：jsdom 里加载不了真 layui，换成可控替身后 `table.render` /
 *   `table.reloadData` / `laydate.render` / `layer.msg` 的**入参**才能被断言；
 * - `vi.mock('@/api/http')`：只换**网络出口**——`@/api/requestMetrics`（URL 与查询串的**唯一**
 *   拼装处）跑的是**真实现** ⇒ 「查询串对不对」这条判据才有意义（把 api 模块也换成替身的话，
 *   它只是在断言自己传了一个对象）。
 *
 * ⚠️ 夹具一律**合成值**（E-0077）：账号名 / 接口路径都不是真租户数据。
 */
const LAYUI = vi.hoisted(() => {
  const msg = vi.fn()
  const tableRenders: Array<Record<string, unknown>> = []
  const reloads: Array<{ id: string; data: unknown }> = []
  const laydateRenders: Array<Record<string, unknown>> = []
  // ⚠️ 这两个数组是**挂载顺序的运行期判据**（`AGENTS.md` §5-G 陷阱 ③）：
  //    记「这两个回调被调用时，页面标记里的输入框在不在 DOM 里」。
  //    ③a 先挂样式 ⇒ bootstrapIsland 看到的是 `false`；③b 标记先塞 ⇒ initIslandElements 看到 `true`。
  //    ⚠️ 选择器这里写**字面值**（不能引页面模块导出的常量）：本工厂在页面模块求值**之前**
  //    就会跑（它是那条 import 链的一环）⇒ 引常量会读到尚未初始化的绑定。
  const seenByBootstrap: boolean[] = []
  const seenByInit: boolean[] = []
  return {
    msg,
    tableRenders,
    reloads,
    laydateRenders,
    seenByBootstrap,
    seenByInit,
    form: { render: vi.fn() },
    table: {
      render: vi.fn((opts: Record<string, unknown>) => {
        tableRenders.push(opts)
      }),
      reloadData: vi.fn((id: string, opts: { data: unknown }) => {
        reloads.push({ id, data: opts.data })
      }),
    },
    laydate: {
      render: vi.fn((opts: Record<string, unknown>) => {
        laydateRenders.push(opts)
      }),
    },
    layer: { msg: vi.fn(), closeAll: vi.fn() },
    $: (el: Element) => el,
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => {
    LAYUI.seenByBootstrap.push(document.querySelector('#perfQueryBtn') !== null)
    return {
      form: LAYUI.form,
      table: LAYUI.table,
      laydate: LAYUI.laydate,
      layer: LAYUI.layer,
      $: LAYUI.$,
    }
  }),
  initIslandElements: vi.fn(() => {
    LAYUI.seenByInit.push(document.querySelector('#perfQueryBtn') !== null)
  }),
  islandMsg: (_layer: unknown, text: string) => LAYUI.msg(text),
}))

/**
 * 只换**网络出口**（见文件头）。`request()` 返回的是**已解包**的 `data`
 * （`@/api/http` 的信封解包在它内部完成）⇒ 替身直接 resolve 一份载荷对象。
 */
vi.mock('@/api/http', () => ({ request: vi.fn() }))

import { request } from '@/api/http'
import { REQUEST_METRICS_URL, requestMetricsUrl } from '@/api/requestMetrics'
import { HttpError } from '@/api/errors'
import page, {
  DATE_FROM_INPUT_ID,
  DATE_TO_INPUT_ID,
  DEFAULT_RANGE_DAYS,
  PANEL_CLASS,
  PANEL_SELECTOR,
  QUERY_BUTTON_ID,
  ROUTE_TABLE_ELEM,
  ROUTE_TABLE_ID,
  SLOW_TABLE_ELEM,
  SLOW_TABLE_ID,
  SUMMARY_ID,
  USER_TABLE_ELEM,
  USER_TABLE_ID,
  __testables,
  personLabel,
  sliceDateTime,
} from '../system-perfanalysis'

const { buildHtml, defaultPerfDateRange, summaryHtml } = __testables

const mockedRequest = vi.mocked(request)

/**
 * 一次响应（键名照 spec §4，**合成值**）。
 *
 * ⚠️ 显式类型标注是**承重的**：它就是「§4 那 5 个键一个不少」的编译期判据——
 * 后端契约里改键名（或有人把 `by_route` 写成 `routes`）时，这份完整字面量会缺键而 `vue-tsc` 报错。
 * ⛔ 别把标注删掉换成 `as`。
 * ⚠️ `by_user` 的**第二行**是匿名（`username: ''` + `user_id: 0`，spec §2 的匿名口径）：
 * 「未登录」那一格必须照实显示，⛔ 不是空白。
 */
const PAYLOAD: RequestMetricsPayload = {
  range: { date_from: '2026-09-28', date_to: '2026-10-04' },
  summary: {
    requests: 1234,
    slow_requests: 7,
    avg_ms: 42,
    max_ms: 1830,
    hist: [
      { label: '<100ms', count: 900 },
      { label: '>=3000ms', count: 3 },
    ],
  },
  by_route: [
    { route: 'api/business/orders/', requests: 100, avg_ms: 120, max_ms: 900 },
    { route: 'api/auth/me/', requests: 80, avg_ms: 9, max_ms: 40 },
  ],
  by_user: [
    { username: 'kefu-perf-a', user_id: 4, requests: 100, avg_ms: 120, max_ms: 900 },
    { username: '', user_id: 0, requests: 5, avg_ms: 10, max_ms: 20 },
  ],
  slowest: [
    {
      occurred_at: '2026-10-04T13:25:57+08:00',
      route: 'api/business/orders/',
      method: 'GET',
      status_code: 200,
      username: 'kefu-perf-a',
      duration_ms: 1830,
    },
  ],
}

/** 第二次查询的响应：数字与上面不同 ⇒ 「响应真的灌进表里」这条判据才分得开。 */
const PAYLOAD_2: RequestMetricsPayload = {
  ...PAYLOAD,
  range: { date_from: '2026-09-20', date_to: '2026-09-21' },
  summary: { ...PAYLOAD.summary, requests: 55, slow_requests: 0, avg_ms: 11, max_ms: 60 },
  by_route: [{ route: 'api/accounts/', requests: 55, avg_ms: 11, max_ms: 60 }],
  by_user: [{ username: 'kefu-perf-b', user_id: 9, requests: 55, avg_ms: 11, max_ms: 60 }],
  slowest: [],
}

function flush(): Promise<void> {
  // 页面的取数链是**多跳**的（点击 → request → 铺四块）：连放几轮宏任务把整条链跑完。
  return (async () => {
    for (let i = 0; i < 5; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
    }
  })()
}

/** 某张表的 `table.render` 配置（按 `elem` 认领；没有就抛，避免静默空判据）。 */
function renderOpts(elem: string): Record<string, unknown> {
  const found = LAYUI.tableRenders.find((r) => r.elem === elem)
  if (!found) throw new Error(`没有对 ${elem} 调用过 table.render`)
  return found
}

/** 某张表的列标题。 */
function titles(elem: string): Array<string | undefined> {
  const cols = renderOpts(elem).cols as Array<Array<{ title?: string }>>
  return cols[0]!.map((c) => c.title)
}

/** 某张表某一列的 `templet`（用它渲染一行，钉「服务端文本怎么上屏」）。 */
function templetOf(elem: string, index: number): (row: never) => string {
  const cols = renderOpts(elem).cols as Array<
    Array<{ templet?: (row: never) => string }>
  >
  const fn = cols[0]![index]!.templet
  if (fn === undefined) throw new Error(`${elem} 的第 ${index} 列没有 templet`)
  return fn
}

/** 最近一次查询实际发出的 URL。 */
function lastUrl(): string {
  const calls = mockedRequest.mock.calls
  const config = calls[calls.length - 1]![0] as { method?: string; url?: string }
  return config.url ?? ''
}

describe('性能分析 island 页', () => {
  let root: HTMLElement

  beforeEach(() => {
    vi.clearAllMocks()
    LAYUI.tableRenders.length = 0
    LAYUI.reloads.length = 0
    LAYUI.laydateRenders.length = 0
    LAYUI.seenByBootstrap.length = 0
    LAYUI.seenByInit.length = 0
    document.body.innerHTML = ''
    root = document.createElement('div')
    document.body.appendChild(root)
    // 日期默认值必须在**确定的一天**上断言字面值（否则用例会随运行日期漂移）：
    // 往回 6 个自然日 = 2026-09-28。
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date(2026, 9, 4, 13, 0, 0))
    mockedRequest.mockResolvedValue(PAYLOAD)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  async function mount(): Promise<void> {
    await page.mount(root)
    await flush()
  }

  // ── 默认时间窗（纯函数 + 上屏值）─────────────────────────────

  it(`默认时间范围 = 最近 ${DEFAULT_RANGE_DAYS} 天（今天与往前 6 个自然日，含今天）`, () => {
    // 用**固定的一天**算，钉字面值：2026-10-04 → 2026-09-28（跨月，9 月 30 天）。
    expect(defaultPerfDateRange(new Date(2026, 9, 4))).toEqual({
      dateFrom: '2026-09-28',
      dateTo: '2026-10-04',
    })
    // 跨月边界再来一个：2026-03-02 → 2026-02-24（2 月 28 天）。
    expect(defaultPerfDateRange(new Date(2026, 2, 2))).toEqual({
      dateFrom: '2026-02-24',
      dateTo: '2026-03-02',
    })
  })

  it('挂载时两个日期框填上默认值，且首屏就用它取了一次数', async () => {
    await mount()
    expect(root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value).toBe('2026-09-28')
    expect(root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value).toBe('2026-10-04')
    expect(mockedRequest).toHaveBeenCalledTimes(1)
    expect(lastUrl()).toBe(
      `${REQUEST_METRICS_URL}?date_from=2026-09-28&date_to=2026-10-04`,
    )
  })

  // ── 驱动真交互（本文件的核心）──────────────────────────────

  it('改日期框 + 点「查询」⇒ 真发 GET 且**查询串逐字正确**（键名 = spec §4 冻结的 date_from / date_to）', async () => {
    await mount()
    mockedRequest.mockClear()
    LAYUI.reloads.length = 0
    mockedRequest.mockResolvedValue(PAYLOAD_2)

    root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value = '2026-09-20'
    root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value = '2026-09-21'
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()

    expect(mockedRequest).toHaveBeenCalledTimes(1)
    const config = mockedRequest.mock.calls[0]![0] as { method?: string; url?: string }
    expect(config.method).toBe('get')
    // ⚠️ 字面值（不引 `requestMetricsUrl(...)` 现算）：现算会让这条断言在**参数名被改坏时**
    //    跟着一起改、照样绿（E-0024 族）。这里的期望是**手写**的。
    expect(config.url).toBe(
      `${REQUEST_METRICS_URL}?date_from=2026-09-20&date_to=2026-09-21`,
    )
    // 顺带钉住「页面上显示的范围 = 发出去的范围」。
    expect(root.querySelector<HTMLElement>(`#${SUMMARY_ID}`)!.innerHTML).toContain('2026-09-20')
  })

  it('响应的三张表**原样灌进**三张表（`reloadData` 的 data 就是返回值）', async () => {
    await mount()
    expect(LAYUI.reloads).toEqual([
      { id: ROUTE_TABLE_ID, data: PAYLOAD.by_route },
      { id: USER_TABLE_ID, data: PAYLOAD.by_user },
      { id: SLOW_TABLE_ID, data: PAYLOAD.slowest },
    ])
    // 再查一次：换一份响应 ⇒ 表里换的是**新返回值**（不是夹具里那批）。
    LAYUI.reloads.length = 0
    mockedRequest.mockResolvedValue(PAYLOAD_2)
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    expect(LAYUI.reloads).toEqual([
      { id: ROUTE_TABLE_ID, data: PAYLOAD_2.by_route },
      { id: USER_TABLE_ID, data: PAYLOAD_2.by_user },
      { id: SLOW_TABLE_ID, data: PAYLOAD_2.slowest },
    ])
  })

  it('两个日期框都清空 ⇒ **一个参数都不发**（后端按它自己的默认窗回，spec §4）', async () => {
    await mount()
    mockedRequest.mockClear()
    root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)!.value = ''
    root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)!.value = ''
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    // 不带 `?`：这是**空参数**的形状（与"发两个空串参数"是两件事——后者后端会判畸形）。
    expect(lastUrl()).toBe(REQUEST_METRICS_URL)
  })

  it('取数失败 ⇒ 如实提示、且页面不清空（不把失败伪装成空结果）', async () => {
    await mount()
    mockedRequest.mockRejectedValue(new HttpError(403, '没有权限执行该操作'))
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    expect(LAYUI.msg).toHaveBeenCalledWith('没有权限执行该操作')
    // 按钮还在（不是"点了没反应"那种静默），概览也没被清空。
    expect(root.querySelector(`#${QUERY_BUTTON_ID}`)).not.toBeNull()
    expect(root.querySelector<HTMLElement>(`#${SUMMARY_ID}`)!.innerHTML).toContain('1234')
  })

  // ── 只读（spec §8 第 3 条）──────────────────────────────────

  it('只读：标记里没有导出 / 写入口，且从未发过非 GET 请求', async () => {
    await mount()
    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.click()
    await flush()
    // ① 请求侧：只有 GET（页面走的是「一条读端点」）。
    const methods = mockedRequest.mock.calls.map(
      (call) => (call[0] as { method?: string }).method,
    )
    expect(methods.length).toBeGreaterThan(0)
    expect(methods.every((m) => m === 'get')).toBe(true)
    // ② 标记侧：⛔ 没有导出 / 删除 / 新增 / 保存这类写入口（spec §8 第 3 条点名"不提供导出"）。
    const html = buildHtml()
    for (const word of ['导出', '删除', '新增', '保存']) {
      expect(html, `标记里出现了不该有的「${word}」入口`).not.toContain(word)
    }
  })

  // ── 三条 island 陷阱（`AGENTS.md` §5-G）────────────────────

  it('三条 island 陷阱：先挂样式 → 再塞标记 → 最后 initIslandElements；类名与选择器分开', async () => {
    await mount()
    // ③a：`bootstrapIsland` 跑的时候页面标记**还不在** DOM 里（顺序反了会先画出没样式的骨架 = FOUC）。
    expect(LAYUI.seenByBootstrap).toEqual([false])
    // ③b：`initIslandElements` 相反，必须在塞标记**之后**（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    expect(LAYUI.seenByInit).toEqual([true])
    // ①：类名常量拼进 `class=`、选择器常量用来查节点——混用会造出 `class=".x"`、**永远匹配不到**。
    const panel = root.querySelector<HTMLElement>(PANEL_SELECTOR)
    expect(panel, `按 ${PANEL_SELECTOR} 匹配不到页面容器（class 里多半混进了点）`).not.toBeNull()
    expect(panel!.classList.contains(PANEL_CLASS)).toBe(true)
    expect(panel!.className).not.toContain('.')
    // ②：以 `(` 开头的语句必须前置分号（`;(table.render as …)(…)`）——这条判据在**本页真跑起来**
    //     时由上面那些用例承载：解析失败会在 mount 阶段直接抛（用例红），不会静默。
    //     这里额外钉住「三个 laydate / 三个 table.render 都真的被调过」，证明那些前置分号的
    //     语句**执行到了**（不是被 ASI 吃掉后面那一句）。
    expect(LAYUI.laydateRenders.map((o) => o.elem)).toEqual([
      `#${DATE_FROM_INPUT_ID}`,
      `#${DATE_TO_INPUT_ID}`,
    ])
    expect(LAYUI.tableRenders.map((o) => o.elem)).toEqual([
      ROUTE_TABLE_ELEM,
      USER_TABLE_ELEM,
      SLOW_TABLE_ELEM,
    ])
  })

  // ── 四块的内容 ─────────────────────────────────────────────

  it('概览：请求数 / 慢请求 / 平均 / 最慢 + 后端给的分档名（前端不镜像词表）', async () => {
    await mount()
    const html = root.querySelector<HTMLElement>(`#${SUMMARY_ID}`)!.innerHTML
    expect(html).toContain('1234')
    expect(html).toContain('7')
    expect(html).toContain('42 ms')
    expect(html).toContain('1830 ms')
    // 分档名来自**后端**（`hist[j].label`）——这里钉的是「后端给什么就显示什么」。
    expect(html).toContain('&lt;100ms：900')
    expect(html).toContain('&gt;=3000ms：3')
  })

  it('三张表的列标题就是那几列（接口 / 人员 / 最慢）', async () => {
    await mount()
    expect(titles(ROUTE_TABLE_ELEM)).toEqual(['接口', '请求数', '平均耗时', '最慢'])
    expect(titles(USER_TABLE_ELEM)).toEqual(['人员', '请求数', '平均耗时', '最慢'])
    expect(titles(SLOW_TABLE_ELEM)).toEqual(['时间', '接口', '方法', '状态码', '人员', '耗时'])
    // 三张表都不翻页（后端已经截了前 50 条，spec §4：⛔ 不做分页器）。
    expect(renderOpts(ROUTE_TABLE_ELEM).page).toBe(false)
    expect(renderOpts(USER_TABLE_ELEM).page).toBe(false)
    expect(renderOpts(SLOW_TABLE_ELEM).page).toBe(false)
    // ⛔ 不给 `url`：给了之后 `reloadData(id, {data})` 会走 ajax 分支、忽略我们传的 data。
    expect(renderOpts(ROUTE_TABLE_ELEM).url).toBeUndefined()
  })

  it('「人员」那一格：匿名行显示「未登录」，账号名照实显示并转义', async () => {
    await mount()
    // 「按人」表的第 0 列就是「人员」。
    const userCell = templetOf(USER_TABLE_ELEM, 0)
    expect(userCell(PAYLOAD.by_user[0] as never)).toBe('kefu-perf-a')
    expect(userCell(PAYLOAD.by_user[1] as never)).toBe('未登录')
    // 纯函数直测：空账号名 + 非 0 的 user_id（后端理论上不会出现，但显示不能塌成空白）。
    expect(personLabel('', 0)).toBe('未登录')
    expect(personLabel('kefu-perf-a', 4)).toBe('kefu-perf-a')
    // 转义：账号名里的尖括号不许当 HTML（它是被 `innerHTML` 插入的）。
    expect(personLabel('<b>x</b>', 4)).toBe('&lt;b&gt;x&lt;/b&gt;')
  })

  it('「最慢」表的时间列（D-0302）：截成 YYYY-MM-DD HH:mm 直显，服务端文本过转义', async () => {
    await mount()
    const timeCell = templetOf(SLOW_TABLE_ELEM, 0)
    expect(
      timeCell({
        occurred_at: '2026-10-04T13:25:57+08:00',
        route: 'api/x/',
        method: 'GET',
        status_code: 200,
        username: '',
        duration_ms: 1,
      } as never),
    ).toBe('2026-10-04 13:25')
    // 纯函数直测：异常短的串原样回（⛔ 不抛错）。
    expect(sliceDateTime('t')).toBe('t')
    // `occurred_at` 来自服务端，templet 返回值当 HTML 插入 ⇒ 尖括号必须已转义。
    expect(
      timeCell({
        occurred_at: '<b>x</b>',
        route: 'api/x/',
        method: 'GET',
        status_code: 200,
        username: '',
        duration_ms: 1,
      } as never),
    ).toBe('&lt;b&gt;x&lt;/b&gt;')
  })

  it('接口列与「最慢」表的接口列都过 escapeHtml（服务端文本不当 HTML 拼）', async () => {
    await mount()
    const routeCell = templetOf(ROUTE_TABLE_ELEM, 0)
    expect(routeCell({ route: 'api/x/', requests: 1, avg_ms: 1, max_ms: 1 } as never)).toBe(
      'api/x/',
    )
    const slowRouteCell = templetOf(SLOW_TABLE_ELEM, 1)
    expect(
      slowRouteCell({
        occurred_at: 't',
        route: '<img src=x>',
        method: 'GET',
        status_code: 200,
        username: '',
        duration_ms: 1,
      } as never),
    ).toBe('&lt;img src=x&gt;')
  })

  // ── 纯函数（URL / 概览）────────────────────────────────────

  it('URL 拼装：参数名是 spec §4 那两个，空值不发、顺序稳定', () => {
    expect(requestMetricsUrl({})).toBe(REQUEST_METRICS_URL)
    expect(requestMetricsUrl({ date_from: '', date_to: '' })).toBe(REQUEST_METRICS_URL)
    expect(requestMetricsUrl({ date_from: ' 2026-09-28 ' })).toBe(
      `${REQUEST_METRICS_URL}?date_from=2026-09-28`,
    )
    expect(requestMetricsUrl({ date_from: '2026-09-28', date_to: '2026-10-04' })).toBe(
      `${REQUEST_METRICS_URL}?date_from=2026-09-28&date_to=2026-10-04`,
    )
  })

  it('summaryHtml 把 range 与 summary 都渲染出来（纯函数直测）', () => {
    const html = summaryHtml(PAYLOAD)
    expect(html).toContain('2026-09-28')
    expect(html).toContain('2026-10-04')
    expect(html).toContain('1234')
    expect(html).toContain('7')
  })
})
