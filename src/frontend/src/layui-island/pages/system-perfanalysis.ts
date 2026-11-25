/**
 * 【开放平台】→「性能分析」页（island key = `system-perfanalysis`）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）里**没有**这一页（`config/replicaRoutes.ts` 的 89 条渲染菜单里只有
 * 「客服接发单统计」与「系统跟踪日志」两页，与请求耗时无关 —— 见计划
 * `docs/plans/2026-10-04-request-metrics.md` §0）⇒ 本页**没有证据件**、也不走
 * `mountConfigLike`（那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。
 * 标记由本文件自己写，风格照 `thirdparty-jidanpu-sync.ts` 那一页的 layui 惯例。
 * （该页已于 2026-10-08 随本线删除，本条仅记先例；⛔ 别去找那个文件。）
 *
 * ## 四块（数据全部来自**同一条**端点，spec §4）
 *
 * 1. **时间范围**（两个日期框，默认**最近 7 天**）+ 一颗「查询」按钮；
 * 2. **按接口**的聚合表（`by_route`，前 50 条）；
 * 3. **按人**的聚合表（`by_user`，前 50 条）；
 * 4. **最慢若干条**的表（`slowest`，前 50 条）。
 *
 * 四块共用 `GET /api/system/request-metrics/` 的**同一次响应**（`summary` 那一段渲染在
 * 时间范围下面的引用块里）⇒ 点一次「查询」只发**一条**请求。
 *
 * ## ⛔ 只读（spec §8 第 3 条，业务裁定）
 *
 * 记录的是**员工维度的耗时**（敏感）⇒ 本页**不提供任何写操作、不提供导出、不提供跨租户汇总**。
 * 别为了「方便排查」加一个「导出 CSV」按钮：那是**新的一次裁定**，不是本页的遗漏。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(table.render as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）。`laydate.render` 也必须在塞标记**之后**
 *    （它按 `#id` 找节点）。
 *
 * ⚠️ 三条都有**运行期**判据（本目录的 `__tests__/system-perfanalysis.spec.ts`），不是靠这段注释。
 */
import {
  fetchRequestMetrics,
  type RequestMetricsByRouteRow,
  type RequestMetricsByUserRow,
  type RequestMetricsPayload,
  type RequestMetricsSlowestRow,
} from '@/api/requestMetrics'
import { formatDateOnly } from '@/layui-island/dateRange'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'system-perfanalysis'
export const TITLE = '性能分析'

/**
 * ISO 时刻串 → `YYYY-MM-DD HH:mm`（慢查询表「时间」列用）。
 *
 * ⚠️ D-0302：后端 `occurred_at` 已是**项目时区**（+08:00）的 ISO 串
 * （`request_metric_views` 的 `local_iso` 口径）⇒ 截断安全；旧写法裸 `field` 直显
 * 整串（秒 + 偏移一起进单元格）。
 */
export function sliceDateTime(value: string): string {
  if (value.length < 16) {
    return value
  }
  return value.substring(0, 16).replace('T', ' ')
}

/** 页面容器 **class 名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'perfPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 两个日期框的 `id`（laydate 的 `elem` 要带 `#`，所以上面再加一层前缀）。 */
export const DATE_FROM_INPUT_ID = 'perfDateFrom'
export const DATE_TO_INPUT_ID = 'perfDateTo'
/** 「查询」按钮的 `id`。 */
export const QUERY_BUTTON_ID = 'perfQueryBtn'
/** 概览（`summary`）那一块的 `id`。 */
export const SUMMARY_ID = 'perfSummary'
/** 三张表的 elem / id：layui 的 `reloadData` 要的是 **id**（带 `#` 是静默 no-op）。 */
export const ROUTE_TABLE_ID = 'perfRouteTable'
export const ROUTE_TABLE_ELEM = '#perfRouteTable'
export const USER_TABLE_ID = 'perfUserTable'
export const USER_TABLE_ELEM = '#perfUserTable'
export const SLOW_TABLE_ID = 'perfSlowTable'
export const SLOW_TABLE_ELEM = '#perfSlowTable'

/**
 * 页面默认时间窗的**天数**（7 = spec §1 第 3 条与 §4 的「最近 7 天」）。
 *
 * ⚠️ 与后端的默认窗（不传 `date_from` ⇒ 最近 7 天，spec §4）是**两个**实现、同一个口径：
 * 本页默认就**显式发**这两个日期（用户看得见自己看到的范围是哪个），所以后端的默认支
 * 只在「用户把两个框都清空再查询」时生效——那一刻两边算的是同一段区间。
 */
export const DEFAULT_RANGE_DAYS = 7

/**
 * 默认时间范围：**今天与往前 6 个自然日**（共 7 个自然日，含今天），形状 `yyyy-MM-dd`。
 *
 * ⚠️ 「最近 7 天」有**两种合理解释**（含今天的 7 个自然日 / 今天往前 7 天、共 8 天）：
 * 这里取前者（`含今天`），理由是页面上的两个框把区间**显示给用户看**，
 * 「今天 ~ 6 天前」比「今天 ~ 7 天前」更贴「最近一周」的直觉。
 * ⛔ 两种解释都说得通 ⇒ 这是**有意选定**的一个值，不是从对标物抄来的（这一页没有对标物）。
 *
 * ⚠️ 一律用 `new Date(y, m, d)` 的**本地**构造器做日期加减（与
 * `@/layui-island/dateRange` 同口径）：`toISOString()` 是 UTC，东八区下会把日期推前一天。
 */
export function defaultPerfDateRange(now: Date = new Date()): {
  dateFrom: string
  dateTo: string
} {
  const from = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() - (DEFAULT_RANGE_DAYS - 1),
  )
  return { dateFrom: formatDateOnly(from), dateTo: formatDateOnly(now) }
}

/**
 * 一行里「人员」那一格（**纯函数，便于测试**）。
 *
 * ⚠️ 后端把**未登录**记成 `user_id = 0` + `username = ''`（spec §2：唯一约束里 `NULL`
 * 互不相等，所以匿名行用 `0` 而不是 `NULL`）⇒ 这里照实写「未登录」，⛔ 不要留空格子
 * （空格子会被读成"这一列没数据"，而它的真实含义是"这些请求没有登录用户"）。
 */
export function personLabel(username: string, userId: number): string {
  if (username !== '') return escapeHtml(username)
  return userId === 0 ? '未登录' : `#${userId}`
}

/**
 * 概览那一块的**纯函数**渲染（`summary` + `range`，键名照 spec §4）。
 *
 * ⚠️ 直方图的 `label` 由**后端给**（`"<100ms"` 这类），前端**不镜像**那张词表：
 * 两处各写 6 个桶名必有一处先漂移，而漂移的症状（页面上的分档名与后端统计的口径不一致）
 * **没有任何网会报**。所以这里只负责把后端给的那几格拼出来。
 * ⚠️ 全部过 `escapeHtml`：`label` 来自服务端、`route` 一类字段将来也可能进这一块，
 * 而它是被 `innerHTML` 插入的。
 */
export function summaryHtml(payload: RequestMetricsPayload): string {
  const summary = payload.summary
  const hist = summary.hist
    .map((bucket) => `${escapeHtml(bucket.label)}：${bucket.count}`)
    .join(' · ')
  return (
    `<span style="color:#999">时间范围：</span>` +
    `<b>${escapeHtml(payload.range.date_from)} ~ ${escapeHtml(payload.range.date_to)}</b>` +
    `<br><span style="color:#999">请求数：</span><b>${summary.requests}</b>` +
    `<span style="color:#999;margin-left:16px">慢请求：</span><b>${summary.slow_requests}</b>` +
    `<span style="color:#999;margin-left:16px">平均耗时：</span><b>${summary.avg_ms} ms</b>` +
    `<span style="color:#999;margin-left:16px">最慢：</span><b>${summary.max_ms} ms</b>` +
    (hist === ''
      ? ''
      : `<br><span style="color:#999">耗时分布：</span>${hist}`)
  )
}

/** 小节标题的内联样式（与 `thirdparty-jidanpu-sync.ts` 的三块同款，⛔ 不发明新类）。
 *  ⚠️ 那个先例文件已于 2026-10-08 随本线删除，本条仅记先例（⛔ 别去找它）——本页的值照旧。 */
const SECTION_STYLE = 'font-size:14px;font-weight:700;margin:16px 0 6px'

/**
 * 页面标记。
 *
 * ⚠️ 本页**没有**「导出」/「删除」/「刷新」之类的写入口（spec §8 第 3 条），
 * 标记里那颗按钮是**唯一**的交互控件。
 */
export function buildHtml(): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="layui-form" style="margin-bottom:6px">
    <div class="layui-inline">
      <label class="layui-form-label" style="width:auto">时间范围</label>
      <div class="layui-input-inline" style="width:150px">
        <input type="text" id="${DATE_FROM_INPUT_ID}" class="layui-input"
               placeholder="开始日期" autocomplete="off">
      </div>
      <div class="layui-form-mid">~</div>
      <div class="layui-input-inline" style="width:150px">
        <input type="text" id="${DATE_TO_INPUT_ID}" class="layui-input"
               placeholder="结束日期" autocomplete="off">
      </div>
      <button type="button" class="layui-btn layui-btn-sm" id="${QUERY_BUTTON_ID}">查询</button>
    </div>
    <div class="layui-form-mid layui-word-aux" style="padding-left:0">
      默认最近 ${DEFAULT_RANGE_DAYS} 天；本页只读，数据按小时汇总、慢请求逐条留存。
    </div>
  </div>

  <blockquote class="layui-elem-quote layui-quote-nm" id="${SUMMARY_ID}">加载中…</blockquote>

  <div style="${SECTION_STYLE}">按接口</div>
  <table id="${ROUTE_TABLE_ID}" lay-filter="${ROUTE_TABLE_ID}"></table>

  <div style="${SECTION_STYLE}">按人</div>
  <table id="${USER_TABLE_ID}" lay-filter="${USER_TABLE_ID}"></table>

  <div style="${SECTION_STYLE}">最慢的请求</div>
  <table id="${SLOW_TABLE_ID}" lay-filter="${SLOW_TABLE_ID}"></table>
</div>`
}

/** 纯函数出口（测试直接用；不依赖 layui）。 */
export const __testables = {
  buildHtml,
  defaultPerfDateRange,
  personLabel,
  summaryHtml,
}

export default {
  mount: async (root) => {
    const layui: LayuiNamespace = await bootstrapIsland(['table', 'layer', 'laydate'])
    const table = layui.table as LayuiModule
    const layer = layui.layer as LayuiModule
    const laydate = layui.laydate as LayuiModule
    const msg = (text: string) => islandMsg(layer as unknown as Record<string, unknown>, text)
    const reload = (id: string, data: unknown) =>
      (table.reloadData as (i: string, o: unknown) => void)(id, { data })

    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    document.title = TITLE
    root.innerHTML = buildHtml()
    initIslandElements(layui)

    const fromInput = root.querySelector<HTMLInputElement>(`#${DATE_FROM_INPUT_ID}`)
    const toInput = root.querySelector<HTMLInputElement>(`#${DATE_TO_INPUT_ID}`)

    /**
     * 两个框的当前值（DOM 是唯一真相；空串 = 不筛这一段，后端按默认窗回）。
     *
     * ⚠️ **本页不做日期格式校验**（有意）：格式的出处是 `laydate` 这个日期控件
     * （`type: 'date'` ⇒ 点选出来的值一定是 `yyyy-MM-dd`），而手输一个畸形值（如 `2026/10/4`）
     * 会由后端按 spec §4 回**业务码 1000**（⛔ 不是 HTML 500）⇒ `request()` 抛 `BizError`、
     * 页面 `layer.msg` 如实显示后端的文案。⛔ 别在这里加"看起来更友好"的前端格式判断：
     * 那会把后端那条「畸形日期必须回 1000」的判据从页面上**遮掉**（它属于 Task 2 的读端点网，
     * 但页面这一侧至少不该假装自己拦住了）。
     */
    const readQuery = (): { date_from: string; date_to: string } => ({
      date_from: fromInput?.value ?? '',
      date_to: toInput?.value ?? '',
    })

    const { dateFrom, dateTo } = defaultPerfDateRange()
    if (fromInput) fromInput.value = dateFrom
    if (toInput) toInput.value = dateTo
    // ⚠️ `laydate.render` 按 `#id` 找节点 ⇒ 必须在**塞标记之后**（顺序与陷阱 ③ 同源）；
    //    也必须在给 `value` **之后**（否则插件初始化时读到的还是空框）。
    ;(laydate.render as (o: unknown) => void)({ elem: `#${DATE_FROM_INPUT_ID}`, type: 'date' })
    ;(laydate.render as (o: unknown) => void)({ elem: `#${DATE_TO_INPUT_ID}`, type: 'date' })

    /**
     * 三张表的列。
     *
     * ⚠️ 文本单元格一律走 `templet` + `escapeHtml`（`route` 是接口模板、`username` 是账号名，
     * 都来自服务端而 `templet` 的返回值是**当 HTML 插入**的）；数字单元格直接拼，
     * 它们来自 JSON 的 number（后端契约里就是整数，spec §4）。
     * ⚠️ `method` / `status_code` 用普通 `field`（layui 2.13.9 的单元格默认
     * `escape: true`，即纯文本转义）——那是我们要的，⛔ 不要给它们加 `templet`。
     * ⚠️ `occurred_at` 走 `templet` + `escapeHtml`（D-0302）：截成
     * `YYYY-MM-DD HH:mm` 直显；`templet` 的返回值当 HTML 插入 ⇒ 必须过 `escapeHtml`。
     */
    const routeCols = [
      {
        field: 'route',
        title: '接口',
        minWidth: 340,
        templet: (row: RequestMetricsByRouteRow) => escapeHtml(row.route),
      },
      { field: 'requests', title: '请求数', width: 120 },
      {
        title: '平均耗时',
        width: 140,
        templet: (row: RequestMetricsByRouteRow) => `${row.avg_ms} ms`,
      },
      {
        title: '最慢',
        width: 140,
        templet: (row: RequestMetricsByRouteRow) => `${row.max_ms} ms`,
      },
    ]
    const userCols = [
      {
        title: '人员',
        minWidth: 200,
        templet: (row: RequestMetricsByUserRow) => personLabel(row.username, row.user_id),
      },
      { field: 'requests', title: '请求数', width: 120 },
      {
        title: '平均耗时',
        width: 140,
        templet: (row: RequestMetricsByUserRow) => `${row.avg_ms} ms`,
      },
      {
        title: '最慢',
        width: 140,
        templet: (row: RequestMetricsByUserRow) => `${row.max_ms} ms`,
      },
    ]
    const slowCols = [
      {
        // ⚠️ D-0302：截成 `YYYY-MM-DD HH:mm` 直显（后端已是 +08:00，截断安全）。
        title: '时间',
        width: 210,
        templet: (row: RequestMetricsSlowestRow) => escapeHtml(sliceDateTime(row.occurred_at)),
      },
      {
        field: 'route',
        title: '接口',
        minWidth: 300,
        templet: (row: RequestMetricsSlowestRow) => escapeHtml(row.route),
      },
      { field: 'method', title: '方法', width: 90 },
      { field: 'status_code', title: '状态码', width: 100 },
      {
        title: '人员',
        width: 160,
        // ⚠️ 契约的 `slowest` 行**没有 `user_id`**（spec §4：只有 occurred_at / route / method /
        // status_code / username / duration_ms）⇒ 第 2 个参数硬编 `0`（= `personLabel` 里
        // 「未登录」那一支）。后果如实登记（本轮 m-14）：`slowest` 的 `username` 是**非空快照**时
        // 直接显示那个名字，`0` 那一支只在 `username === ''`（匿名请求）时生效；
        // ⛔ 想在这一列显示「#用户ID」得先改后端契约（本批不动）。
        templet: (row: RequestMetricsSlowestRow) => personLabel(row.username, 0),
      },
      {
        title: '耗时',
        width: 120,
        templet: (row: RequestMetricsSlowestRow) => `${row.duration_ms} ms`,
      },
    ]

    // ⚠️ 先 render 一次（哪怕空数据）：layui 的实例是在 render 时注册的，而
    //    `reloadData(id, {data})` 靠这个实例把数据灌进去（本页**不给** `url`：
    //    给了之后 `reloadData` 会走 ajax 分支、忽略我们传的 `data`）。
    ;(table.render as (o: unknown) => void)({
      elem: ROUTE_TABLE_ELEM,
      data: [] as RequestMetricsByRouteRow[],
      defaultToolbar: [],
      cols: [routeCols],
      page: false,
      text: { none: '这段时间里没有请求记录' },
    })
    ;(table.render as (o: unknown) => void)({
      elem: USER_TABLE_ELEM,
      data: [] as RequestMetricsByUserRow[],
      defaultToolbar: [],
      cols: [userCols],
      page: false,
      text: { none: '这段时间里没有请求记录' },
    })
    ;(table.render as (o: unknown) => void)({
      elem: SLOW_TABLE_ELEM,
      data: [] as RequestMetricsSlowestRow[],
      defaultToolbar: [],
      cols: [slowCols],
      page: false,
      text: { none: '这段时间里没有慢请求' },
    })

    /** 把**同一次**响应铺到四块上（概览 + 三张表）。 */
    function applyPayload(payload: RequestMetricsPayload): void {
      const summary = root.querySelector<HTMLElement>(`#${SUMMARY_ID}`)
      if (summary) summary.innerHTML = summaryHtml(payload)
      reload(ROUTE_TABLE_ID, payload.by_route)
      reload(USER_TABLE_ID, payload.by_user)
      reload(SLOW_TABLE_ID, payload.slowest)
    }

    /**
     * 取数并上屏（两个日期框的**当前值**就是筛选条件）。
     *
     * ⚠️ 失败**不吞**：`request()` 抛的类型化错误一路交到调用方（初始加载与点「查询」
     * 各自 `catch` 后 `layer.msg`）。空结果 ≠ 取数失败（`AGENTS.md` §5-E）：
     * 那三张表的 `text.none` 文案才是"真的没有数据"的出口。
     */
    async function load(): Promise<void> {
      applyPayload(await fetchRequestMetrics(readQuery()))
    }

    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.addEventListener('click', () => {
      void load().catch((error: Error) => msg(error.message))
    })

    try {
      // 首屏就取一次（否则用户看到的是三个空表 + "加载中…"）。
      await load()
    } catch (error) {
      msg((error as Error).message)
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
