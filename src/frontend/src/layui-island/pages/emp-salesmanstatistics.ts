/**
 * 【客服接发单统计】页（island key = `emp-salesmanstatistics`，路由 `/agents/salesman-stats`）。
 *
 * ## 这一页是**自有实现**，不是复刻
 *
 * 在此之前它是一张**证据件渲染页**：走 `artifactPage` 取 `public/replica/emp-salesmanstatistics.html`
 * 照对标 1:1 渲染 20 列、**零 API 调用**，所有按钮点了只会提示「尚未开放」。
 *
 * 业务方 2026-10-17 原话（spec `docs/specs/2026-10-17-support-stats-and-business-report-design.md` §0）：
 * 「不需要照对比设计的去做，我们按照自己的要求来重新实现这个功能。首先 操作人员、总订单数、
 * 总接单价 这几个保留，然后新增总发单价，预计绩效（这里的预计绩效用接单价减去发单价即可），
 * 只要这几个字段即可，然后我们新增一个店铺统计功能也就是订单来源，按照订单来源也统计
 * 总订单数 总接单价 总发单价。」
 *
 * ⇒ 本页**不再有对标证据件**，标记由本文件自己写（风格照 `system-perfanalysis.ts` 那一页的
 * layui 惯例），列与口径照 spec §4，数据来自我们自己的 `GET /api/business/support-stats`。
 * ⚠️ 旧证据件 `public/replica/emp-salesmanstatistics.html` **还在**（本次文件清单没让删它），
 * 但本页已**不再引用**它 —— 别把 `artifactPage` / `ARTIFACT_URL` 加回来。
 *
 * ## 两块（数据来自**同一条**端点，spec §6.1）
 *
 * 1. **操作人员接发单统计**：操作人员 / 总订单数 / 总接单价 / 总发单价 / 预计绩效；
 * 2. **店铺（订单来源）统计**：店铺 / 总订单数 / 总接单价 / 总发单价。
 *
 * 两张表都 `page:false`（一次给全量）+ `totalRow:true`（合计行由 layui 本地相加，
 * 与后端给的 `operators_total` / `sources_total` 同一批数据、同一个式子）+ `defaultToolbar:['filter']`，
 * 空数据文案「这段时间没有订单」。
 *
 * ## 金额一律**字符串直显**
 *
 * 接口把 `Decimal` 序列化成两位小数字符串（`"300.00"`，spec §2）⇒ 页面**不做**任何浮点运算、
 * 也不 `toFixed` 一次：JS 的 `Number` 会把 `"0.10"` 与 `"0.20"` 算成 `0.30000000000000004`。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(table.render as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）。`laydate.render` 也必须在塞标记
 *    **之后**（它按 `#id` 找节点），且要在给两个框**填完默认值之后**（插件初始化时读的是当时的值）。
 *
 * ⚠️ 三条都有**运行期**判据（本目录的 `__tests__/emp-salesmanstatistics.spec.ts`），不是靠这段注释。
 */
import {
  fetchSupportStats,
  type SupportStatsOperatorRow,
  type SupportStatsPayload,
  type SupportStatsSourceRow,
} from '@/api/supportStats'
import { formatDateOnly } from '@/layui-island/dateRange'
import { escapeHtml } from '@/layui-island/escape'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'emp-salesmanstatistics'
/** 页面标题：**菜单名**（spec §4：原来是证据件的「客服销售和发单统计明细」，本次改成「客服接发单统计」）。 */
export const TITLE = '客服接发单统计'

/** 页面容器 **class 名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'empStatsPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`

/** 两个日期框的 `id`（laydate 的 `elem` 要带 `#`，所以查节点时再加一层前缀）。 */
export const DATE_FROM_INPUT_ID = 'empStatsDateFrom'
export const DATE_TO_INPUT_ID = 'empStatsDateTo'
/** 「查询」按钮的 `id`。 */
export const QUERY_BUTTON_ID = 'empStatsQueryBtn'
/** 两张表的 elem / id：layui 的 `reloadData` 要的是 **id**（带 `#` 是静默 no-op）。 */
export const OPERATOR_TABLE_ID = 'empStatsOperatorTable'
export const OPERATOR_TABLE_ELEM = '#empStatsOperatorTable'
export const SOURCE_TABLE_ID = 'empStatsSourceTable'
export const SOURCE_TABLE_ELEM = '#empStatsSourceTable'

/** 空数据文案（两张表共用；spec §4「空数据文案『这段时间没有订单』」）。 */
export const EMPTY_TEXT = '这段时间没有订单'

/**
 * 默认时间窗的**天数**（spec §4：默认最近 30 天，含今天）。
 *
 * ⚠️ 与后端的默认窗（不传 `date_from` / `date_to` ⇒ 最近 30 天，spec §6.1）是**两个**实现、
 * 同一个口径：本页默认就**显式发**这两个日期（用户看得见自己看到的范围是哪个），
 * 所以后端那条默认支只在「用户把两个框都清空再查询」时生效——那一刻两边算的是同一段区间。
 */
export const DEFAULT_RANGE_DAYS = 30

/**
 * 默认时间范围：**今天与往前 29 个自然日**（共 30 个自然日，含今天），形状 `yyyy-MM-dd`。
 *
 * ⚠️ 一律用 `new Date(y, m, d)` 的**本地**构造器做日期加减（与 `@/layui-island/dateRange` 同口径）：
 * `toISOString()` 是 UTC，东八区下会把日期推前一天。
 * ⚠️ 「最近 30 天」取「含今天的 30 个自然日」（`today-29d ~ today`），与实名认证页
 * （`dayRangeDaysAgo(29)`）同一读法。
 */
export function defaultDateRange(now: Date = new Date()): {
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
 * 一张表的列声明形状（layui `table.render` 的 `cols[0]`）。
 *
 * ⚠️ 只声明本页用到的几种属性；`templet` 的返回值是**当 HTML 插入**的，所以文本列必须
 * 自己过 `escapeHtml`（见 `operatorCols`）。
 */
export type SupportStatsCol<Row> = {
  field?: string
  title: string
  width?: number
  minWidth?: number
  align?: 'left' | 'center' | 'right'
  /** 表级 `totalRow:true` 时，这一列要不要本地求和。 */
  totalRow?: boolean
  /** 不求和的那一列（操作人员 / 店铺）在合计行里显示的字样。 */
  totalRowText?: string
  templet?: (row: Row) => string
}

/**
 * 卡 1「操作人员接发单统计」的列（spec §4）。
 *
 * ⚠️ 金额列**不做 `templet`**：接口给的就是两位小数字符串，原样显示即正确；加一层
 * `toFixed` 反而会引入浮点误差。数字列右对齐（计划 §4「数字列右对齐」）。
 * ⚠️ 第一列只有 `totalRowText`、**没有** `totalRow`（合计行里写「合计」，不参与求和）。
 */
export function operatorCols(): Array<SupportStatsCol<SupportStatsOperatorRow>> {
  return [
    {
      field: 'name',
      title: '操作人员',
      minWidth: 160,
      totalRowText: '合计',
      // 名字来自服务端（显示名 / `sales_agent` 文本快照）⇒ 必须转义（templet 的返回值当 HTML 插）。
      templet: (row) => escapeHtml(row.name),
    },
    { field: 'order_count', title: '总订单数', width: 120, align: 'right', totalRow: true },
    { field: 'total_price', title: '总接单价', width: 140, align: 'right', totalRow: true },
    { field: 'total_hall_price', title: '总发单价', width: 140, align: 'right', totalRow: true },
    { field: 'performance', title: '预计绩效', width: 140, align: 'right', totalRow: true },
  ]
}

/**
 * 卡 2「店铺（订单来源）统计」的列（spec §4）。
 *
 * ⚠️ 这一张**没有**「预计绩效」列：业务方只点名了 总订单数 / 总接单价 / 总发单价
 * （spec §0 原话），⛔ 别「顺手」补上第 4 列。
 */
export function sourceCols(): Array<SupportStatsCol<SupportStatsSourceRow>> {
  return [
    {
      field: 'name',
      title: '店铺',
      minWidth: 200,
      totalRowText: '合计',
      templet: (row) => escapeHtml(row.name),
    },
    { field: 'order_count', title: '总订单数', width: 120, align: 'right', totalRow: true },
    { field: 'total_price', title: '总接单价', width: 140, align: 'right', totalRow: true },
    { field: 'total_hall_price', title: '总发单价', width: 140, align: 'right', totalRow: true },
  ]
}

/** 页面标记（**没有**任何写入口：这一页是只读统计页）。 */
export function buildHtml(): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="layui-form" style="margin-bottom:6px">
    <div class="layui-inline">
      <label class="layui-form-label" style="width:auto">统计时间</label>
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
      默认最近 ${DEFAULT_RANGE_DAYS} 天；按录单时间统计，金额单位：元。
    </div>
  </div>

  <section class="layui-card">
    <div class="layui-card-header">操作人员接发单统计</div>
    <div class="layui-card-body">
      <table id="${OPERATOR_TABLE_ID}" lay-filter="${OPERATOR_TABLE_ID}"></table>
    </div>
  </section>

  <section class="layui-card">
    <div class="layui-card-header">店铺（订单来源）统计</div>
    <div class="layui-card-body">
      <table id="${SOURCE_TABLE_ID}" lay-filter="${SOURCE_TABLE_ID}"></table>
    </div>
  </section>
</div>`
}

/**
 * 合计行的单元格文本（**后端算好的** `operators_total` / `sources_total`）。
 *
 * 为什么不靠 layui 的 `totalRow` 本地求和：`renderTotal` 的写法是
 * `s[e] ? parseFloat(...).toFixed(2) : ""` ⇒ **合计为 0 时该格是空白**，而后端给的是
 * `"0.00"`（独立评审 2026-10-17 I1）。这里直接把后端合计铺到 layui 的合计行上，
 * 顺带保证「页面合计 = 后端合计」是同一份数据。
 */
export function operatorTotalValues(total: SupportStatsOperatorRow): string[] {
  return [
    total.name,
    String(total.order_count),
    total.total_price,
    total.total_hall_price,
    total.performance,
  ]
}

export function sourceTotalValues(total: SupportStatsSourceRow): string[] {
  return [total.name, String(total.order_count), total.total_price, total.total_hall_price]
}

/** 纯函数出口（测试直接用；不依赖 layui）。 */
export const __testables = {
  buildHtml,
  defaultDateRange,
  operatorCols,
  operatorTotalValues,
  sourceCols,
  sourceTotalValues,
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
     * ⚠️ 本页**不做**日期格式校验（有意，与 `system-perfanalysis.ts` 同一口径）：格式的出处是
     * `laydate`（`type:'date'` ⇒ 点选出来一定是 `yyyy-MM-dd`）；手输畸形值由后端回**业务码 1000**，
     * `request()` 抛 `BizError`、页面如实显示后端文案。⛔ 别在这里加前端兜底判断——
     * 那会把后端那条「畸形日期必须回 1000」的判据从页面上遮掉。
     */
    const readQuery = (): { dateFrom: string; dateTo: string } => ({
      dateFrom: fromInput?.value ?? '',
      dateTo: toInput?.value ?? '',
    })

    const { dateFrom, dateTo } = defaultDateRange()
    if (fromInput) fromInput.value = dateFrom
    if (toInput) toInput.value = dateTo
    // ⚠️ `laydate.render` 按 `#id` 找节点 ⇒ 必须在塞标记**之后**（陷阱 ③ 同源），
    //    也必须在填完默认值**之后**（插件初始化时读到的还是空框）。
    ;(laydate.render as (o: unknown) => void)({ elem: `#${DATE_FROM_INPUT_ID}`, type: 'date' })
    ;(laydate.render as (o: unknown) => void)({ elem: `#${DATE_TO_INPUT_ID}`, type: 'date' })

    // ⚠️ 先 render 一次（哪怕空数据）：layui 的实例是在 render 时注册的，而
    //    `reloadData(id, {data})` 靠这个实例把数据灌进去（本页**不给** `url`：
    //    给了之后 `reloadData` 会走 ajax 分支、忽略我们传的 `data`）。
    ;(table.render as (o: unknown) => void)({
      elem: OPERATOR_TABLE_ELEM,
      data: [] as SupportStatsOperatorRow[],
      totalRow: true,
      defaultToolbar: ['filter'],
      cols: [operatorCols()],
      page: false,
      text: { none: EMPTY_TEXT },
    })
    ;(table.render as (o: unknown) => void)({
      elem: SOURCE_TABLE_ELEM,
      data: [] as SupportStatsSourceRow[],
      totalRow: true,
      defaultToolbar: ['filter'],
      cols: [sourceCols()],
      page: false,
      text: { none: EMPTY_TEXT },
    })

    /** 合计行文本（后端给的那两份；`null` = 还没取到数）。 */
    let operatorTotal: SupportStatsOperatorRow | null = null
    let sourceTotal: SupportStatsSourceRow | null = null

    /**
     * 把后端合计铺到 layui 的合计行上（理由见 `operatorTotalValues` 的注释）。
     *
     * ⚠️ `reloadData` 是同步渲染，但合计行节点在 `renderData` 末尾才插进去 ⇒ 用
     * `setTimeout(0)` 让这一跳落在 DOM 更新之后；找不到节点就静默跳过（例如空表时
     * layui 把合计行隐藏了，此时不覆盖反而对）。
     */
    function fillTotalRows(): void {
      const views = root.querySelectorAll<HTMLElement>('.layui-table-view')
      const fill = (view: HTMLElement | undefined, values: string[]): void => {
        if (!view) return
        const cells = view.querySelectorAll<HTMLElement>('.layui-table-total .layui-table-cell')
        values.forEach((value, index) => {
          const cell = cells[index]
          if (cell) cell.textContent = value
        })
      }
      if (operatorTotal) fill(views[0], operatorTotalValues(operatorTotal))
      if (sourceTotal) fill(views[1], sourceTotalValues(sourceTotal))
    }

    /** 把**同一次**响应铺到两张表上。 */
    function applyPayload(payload: SupportStatsPayload): void {
      operatorTotal = payload.operators_total
      sourceTotal = payload.sources_total
      reload(OPERATOR_TABLE_ID, payload.operators)
      reload(SOURCE_TABLE_ID, payload.sources)
      window.setTimeout(fillTotalRows, 0)
    }

    /**
     * 取数并上屏（两个日期框的**当前值**就是筛选条件）。
     *
     * ⚠️ 失败**不吞**：初始加载与点「查询」各自 `catch` 后 `layer.msg`。
     * 空结果 ≠ 取数失败（`AGENTS.md` §5-E）：两张表的 `text.none` 文案才是「真的没有数据」的出口。
     */
    let loadSeq = 0
    async function load(): Promise<void> {
      const seq = ++loadSeq
      try {
        const payload = await fetchSupportStats(readQuery())
        // ⚠️ 连点「查询」时，后返回的旧条件响应**不许覆盖**新结果（独立评审 2026-10-17 I4）。
        if (seq === loadSeq) applyPayload(payload)
      } catch (error) {
        // 旧请求的失败也不提示：它已经被更新的一次查询取代了。
        if (seq === loadSeq) throw error
      }
    }

    root.querySelector<HTMLButtonElement>(`#${QUERY_BUTTON_ID}`)!.addEventListener('click', () => {
      void load().catch((error: Error) => msg(error.message))
    })

    try {
      // 首屏就取一次（否则用户看到的是两张空表）。
      await load()
    } catch (error) {
      msg((error as Error).message)
    }

    return () => {
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
